/*
 * Teste de estabilidade por núcleo do Monitor de Lentidão.
 *
 * Usado pelo ajuste automático do processador (Curve Optimizer no Ryzen, undervolt no Intel). Cada bloco de
 * trabalho é calculado duas vezes com a mesma semente: num núcleo estável o resultado é idêntico bit a bit;
 * qualquer diferença é erro de hardware (tensão baixa demais para aquele núcleo naquela frequência).
 * Roda na thread que chamou; quem chama prende a thread no núcleo desejado.
 *
 * Modos: 0 = ponto flutuante pesado (AVX2/FMA quando houver), 1 = inteiros e cache, 2 = carga que liga e
 * desliga (o boost sobe e desce, onde o Curve Optimizer costuma falhar), 3 = mistura dos três.
 *
 * ram_testar: teste de estabilidade da RAM feito para timings apertados. Alterna dois padrões num bloco próprio:
 * acesso aleatório (cada palavra numa linha/banco diferente: força tRCD, tRP, tRAS, tRRD, tFAW) e inversões
 * móveis em sequência (bits 0101/1010 indo e voltando: força o barramento e o tWR/tWTR).
 *
 * Windows:  x86_64-w64-mingw32-gcc -O3 -shared -static -s -o estavel.dll estavel.c
 * Linux:    gcc -O3 -shared -fPIC -o libestavel.so estavel.c
 */

#include <stdint.h>
#include <string.h>

#ifdef _WIN32
#include <windows.h>
#define EXPORTA __declspec(dllexport)
#else
#include <stdlib.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>
#define EXPORTA __attribute__((visibility("default")))
#endif

static double agora_ms(void) {
#ifdef _WIN32
    static LARGE_INTEGER freq;
    LARGE_INTEGER t;
    if (!freq.QuadPart) QueryPerformanceFrequency(&freq);
    QueryPerformanceCounter(&t);
    return (double)t.QuadPart * 1000.0 / (double)freq.QuadPart;
#else
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec * 1000.0 + t.tv_nsec * 1e-6;
#endif
}

static void dormir_ms(int ms) {
#ifdef _WIN32
    Sleep((DWORD)ms);
#else
    usleep((useconds_t)ms * 1000);
#endif
}

static inline uint64_t mistura(uint64_t *s) {  /* splitmix64 */
    uint64_t z = (*s += 0x9E3779B97F4A7C15ull);
    z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ull;
    z = (z ^ (z >> 27)) * 0x94D049BB133111EBull;
    return z ^ (z >> 31);
}

static inline uint64_t rotl(uint64_t x, int k) { return (x << k) | (x >> (64 - k)); }

/* ------------------------------------------------------------------ ponto flutuante */
#define N_FP 256

#define CORPO_FP                                                                     \
    double x[N_FP], y[N_FP];                                                         \
    uint64_t s = semente;                                                            \
    for (int i = 0; i < N_FP; i++) {                                                 \
        x[i] = 0.5 + (double)(mistura(&s) >> 11) * (1.0 / 9007199254740992.0);       \
        y[i] = 0.5 + (double)(mistura(&s) >> 11) * (1.0 / 9007199254740992.0);       \
    }                                                                                \
    for (int it = 0; it < iter; it++) {                                              \
        for (int i = 0; i < N_FP; i++) {                                             \
            double a = x[i] * 0.99999991 + y[i] * 1.3e-7;                            \
            double b = y[i] * 1.00000007 - x[i] * 0.9e-7;                            \
            x[i] = a * 1.0000000001 + 1e-12;                                         \
            y[i] = b * 0.9999999999 - 1e-12;                                         \
        }                                                                            \
    }                                                                                \
    uint64_t h = semente;                                                            \
    for (int i = 0; i < N_FP; i++) {                                                 \
        uint64_t a, b;                                                               \
        memcpy(&a, &x[i], 8);                                                        \
        memcpy(&b, &y[i], 8);                                                        \
        h = rotl(h ^ a, 17) * 0x9E3779B97F4A7C15ull ^ b;                             \
    }                                                                                \
    return h;

static uint64_t bloco_fp_base(uint64_t semente, int iter) { CORPO_FP }

#if defined(__GNUC__) && (defined(__x86_64__) || defined(_M_X64))
__attribute__((target("avx2,fma"))) static uint64_t bloco_fp_avx2(uint64_t semente, int iter) { CORPO_FP }
#define TEM_AVX2_FN 1
#endif

static int usar_avx2 = -1;

static uint64_t bloco_fp(uint64_t semente, int iter) {
#ifdef TEM_AVX2_FN
    if (usar_avx2 < 0) {
        __builtin_cpu_init();
        usar_avx2 = __builtin_cpu_supports("avx2") && __builtin_cpu_supports("fma");
    }
    if (usar_avx2) return bloco_fp_avx2(semente, iter);
#endif
    return bloco_fp_base(semente, iter);
}

/* ------------------------------------------------------------------ inteiros e cache */
#define N_INT (64 * 1024)  /* 256 KB: cabe no cache L2 */
static uint32_t ciclo[N_INT];
static int ciclo_pronto = 0;

static void preparar_ciclo(void) {
    /* permutação aleatória num ciclo único (algoritmo de Sattolo), igual em toda chamada */
    uint64_t s = 12345;
    for (uint32_t i = 0; i < N_INT; i++) ciclo[i] = i;
    for (uint32_t i = N_INT - 1; i > 0; i--) {
        uint32_t j = (uint32_t)(mistura(&s) % i);
        uint32_t t = ciclo[i]; ciclo[i] = ciclo[j]; ciclo[j] = t;
    }
    ciclo_pronto = 1;
}

static uint64_t bloco_int(uint64_t semente, int iter) {
    uint64_t h = semente, g = ~semente;
    uint32_t p = (uint32_t)(semente % N_INT);
    for (int it = 0; it < iter; it++) {
        p = ciclo[p];
        h ^= p;
        h *= 0x9E3779B97F4A7C15ull;
        h = rotl(h, 29) + (uint64_t)it;
        g += h ^ (g >> 7);
        g = rotl(g, 11) * 0xD6E8FEB86659FD93ull;
    }
    return h ^ g;
}

/* ------------------------------------------------------------------ laço */
static uint64_t calcular(int tipo, uint64_t semente) {
    return tipo == 0 ? bloco_fp(semente, 600) : bloco_int(semente, 120000);
}

/* Roda por `ms` milissegundos. Retorna quantos blocos deram resultados diferentes nas duas contas.
 * `ops` recebe o total de blocos conferidos; `parar` (opcional) interrompe quando vira diferente de zero. */
EXPORTA int estavel_rodar(int ms, int modo, uint64_t semente, uint64_t *ops, volatile int *parar) {
    if (!ciclo_pronto) preparar_ciclo();
    int erros = 0;
    uint64_t feitos = 0;
    double fim = agora_ms() + ms, troca = 0, pausa_ate = 0;
    int tipo = modo == 1 ? 1 : 0, ligado = 1;
    uint64_t s = semente ? semente : 0xC0FFEE;
    while (agora_ms() < fim) {
        if (parar && *parar) break;
        double t = agora_ms();
        if (modo == 2 || modo == 3) {
            /* liga e desliga: rajadas de 3 a 25 ms com pausas de 2 a 30 ms */
            if (t >= troca) {
                ligado = !ligado;
                uint64_t r = mistura(&s);
                troca = t + (ligado ? 3 + (double)(r % 23) : 2 + (double)(r % 29));
                if (modo == 3 && ligado && (r >> 40) % 7 == 0) tipo = !tipo;
            }
            if (!ligado) {
                if (t < pausa_ate) continue;
                int d = (int)(troca - t);
                if (d > 0) dormir_ms(d);
                pausa_ate = troca;
                continue;
            }
        }
        uint64_t semente_bloco = mistura(&s);
        uint64_t a = calcular(tipo, semente_bloco);
        uint64_t b = calcular(tipo, semente_bloco);
        if (a != b) erros++;
        feitos++;
        if (modo == 3 && (feitos % 64) == 0) tipo = !tipo;
    }
    if (ops) *ops = feitos;
    return erros;
}

/* Conta de referência: o resultado de um bloco conhecido, para conferir que a DLL calcula igual em qualquer PC. */
EXPORTA uint64_t estavel_referencia(int tipo) {
    if (!ciclo_pronto) preparar_ciclo();
    return calcular(tipo, 0x5EED);
}

/* ------------------------------------------------------------------ RAM */
static void *aloca_ram(size_t bytes) {
#ifdef _WIN32
    return VirtualAlloc(NULL, bytes, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
#else
    void *p = mmap(NULL, bytes, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    return p == MAP_FAILED ? NULL : p;
#endif
}

static void libera_ram(void *p, size_t bytes) {
#ifdef _WIN32
    (void)bytes;
    VirtualFree(p, 0, MEM_RELEASE);
#else
    munmap(p, bytes);
#endif
}

static inline uint64_t valor_em(uint64_t i, uint64_t s) {  /* conteúdo esperado de cada palavra numa passada */
    uint64_t z = (i + 1) * 0x9E3779B97F4A7C15ull ^ s;
    z = (z ^ (z >> 29)) * 0xBF58476D1CE4E5B9ull;
    return z ^ (z >> 32);
}

#define TRECHO (1u << 18)  /* palavras entre uma conferência de tempo e outra */

/* Testa `bytes` de RAM (arredondado para potência de 2) por `ms` milissegundos. Retorna quantas palavras
 * voltaram diferentes do que foi gravado (-1 se não conseguiu alocar). `conferidas` soma, durante o teste, as
 * palavras de 8 bytes já lidas e conferidas. */
#define CONTA(qtd) do { if (conferidas) *conferidas += (qtd); } while (0)
EXPORTA int64_t ram_testar(int64_t bytes, int ms, uint64_t semente, volatile uint64_t *conferidas, volatile int *parar) {
    uint64_t n = 1;
    while (n * 2 * 8 <= (uint64_t)bytes) n *= 2;  /* palavras: potência de 2, para a permutação cobrir tudo */
    if (n < 4096) return -1;
    size_t tamanho = (size_t)(n * 8);
    uint64_t *m = aloca_ram(tamanho);
    if (!m) return -1;
    int64_t erros = 0;
    double fim = agora_ms() + ms;
    uint64_t s = semente ? semente : 0xA5A5A5A5ull, passada = 0, mascara = n - 1;
    int acabou = 0;
    while (!acabou) {
        passada++;
        s = mistura(&s);
        /* 1) aleatório: grava numa ordem embaralhada, confere em outra */
        uint64_t p1 = (mistura(&s) | 1) & mascara, p2 = (mistura(&s) | 1) & mascara;
        if (p1 < 3) p1 = 0x9E37 | 1;
        if (p2 < 3) p2 = 0x7F4B | 1;
        for (uint64_t k = 0; k < n && !acabou; k++) {
            uint64_t i = (k * p1) & mascara;
            m[i] = valor_em(i, s);
            if ((k & (TRECHO - 1)) == 0 && ((parar && *parar) || agora_ms() >= fim)) acabou = 1;
        }
        if (acabou) break;
#ifdef INJETAR_FALHA  /* só nos testes: simula um bit que a RAM perdeu */
        m[(passada * 7919) & mascara] ^= 1ull << (passada & 63);
#endif
        for (uint64_t k = 0; k < n; k++) {
            uint64_t i = (k * p2) & mascara;
            if (((volatile uint64_t *)m)[i] != valor_em(i, s)) erros++;
            if ((k & (TRECHO - 1)) == TRECHO - 1) {
                CONTA(TRECHO);
                if ((parar && *parar) || agora_ms() >= fim) { acabou = 1; break; }
            }
        }
        if (acabou) break;
        /* 2) inversões móveis: 0101.. subindo, confere e grava o inverso, depois desce */
        uint64_t padrao = (passada & 1) ? 0x5555555555555555ull : 0x3333333333333333ull;
        padrao ^= s & 0xFF00FF00FF00FF00ull;
        for (uint64_t i = 0; i < n; i++) m[i] = padrao;
        for (uint64_t i = 0; i < n; i++) {
            if (((volatile uint64_t *)m)[i] != padrao) erros++;
            m[i] = ~padrao;
            if ((i & (TRECHO - 1)) == TRECHO - 1) {
                CONTA(TRECHO);
                if ((parar && *parar) || agora_ms() >= fim) { acabou = 1; break; }
            }
        }
        if (acabou) break;
        for (uint64_t i = n; i-- > 0;) {
            if (((volatile uint64_t *)m)[i] != ~padrao) erros++;
            m[i] = padrao;
            if ((i & (TRECHO - 1)) == 0) {
                CONTA(TRECHO);
                if ((parar && *parar) || agora_ms() >= fim) { acabou = 1; break; }
            }
        }
        if (acabou) break;
        for (uint64_t i = 0; i < n; i++)
            if (((volatile uint64_t *)m)[i] != padrao) erros++;
        CONTA(n);
        if ((parar && *parar) || agora_ms() >= fim) break;
    }
    libera_ram(m, tamanho);
    return erros;
}

EXPORTA int estavel_versao(void) { return 2; }
