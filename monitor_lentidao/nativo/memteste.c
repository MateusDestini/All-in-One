/*
 * Teste de memória do Monitor de Lentidão.
 *
 * Mede latência (pointer chasing em ordem aleatória) e banda de leitura, escrita
 * e cópia, usando as mesmas técnicas conhecidas dos benchmarks de memória como o
 * do AIDA64: instruções SIMD largas (AVX-512, AVX2 ou SSE2, conforme o processador),
 * escrita "non-temporal" direto na RAM, uma thread presa a cada núcleo físico e
 * páginas grandes quando o Windows permite.
 * Fica em C porque Python é lento demais para medir nanossegundos.
 *
 * Windows:  x86_64-w64-mingw32-gcc -O2 -shared -static -s -o memteste.dll memteste.c
 * Linux:    gcc -O2 -shared -fPIC -o libmemteste.so memteste.c -lpthread -lm
 */

#define _GNU_SOURCE
#include <immintrin.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#ifdef _WIN32
#include <windows.h>
#define EXPORTA __declspec(dllexport)
#else
#include <pthread.h>
#include <sched.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>
#define EXPORTA __attribute__((visibility("default")))
#endif

#define PAGINA 4096
#define MAX_FIOS 64

static double agora_s(void) {
#ifdef _WIN32
    static LARGE_INTEGER freq;
    LARGE_INTEGER t;
    if (!freq.QuadPart) QueryPerformanceFrequency(&freq);
    QueryPerformanceCounter(&t);
    return (double)t.QuadPart / (double)freq.QuadPart;
#else
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec + t.tv_nsec * 1e-9;
#endif
}

/* ------------------------------------------------------------------ memória */

static void *aloca(size_t bytes) {
#ifdef _WIN32
    return VirtualAlloc(NULL, bytes, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
#else
    void *p = mmap(NULL, bytes, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    return p == MAP_FAILED ? NULL : p;
#endif
}

static void libera(void *p, size_t bytes) {
    if (!p) return;
#ifdef _WIN32
    (void)bytes;
    VirtualFree(p, 0, MEM_RELEASE);
#else
    munmap(p, bytes);
#endif
}

/*
 * Páginas grandes (2 MB): a tradução de endereços quase não pesa e a latência medida fica
 * mais próxima da real. No Windows exige o direito "Bloquear páginas na memória"
 * (SeLockMemoryPrivilege); sem ele, cai para páginas normais e avisa por `*grande = 0`.
 */
static void *aloca_grande(size_t *bytes, int *grande) {
    *grande = 0;
#ifdef _WIN32
    HANDLE token;
    if (OpenProcessToken(GetCurrentProcess(), TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY, &token)) {
        TOKEN_PRIVILEGES tp = {1, {{{0, 0}, SE_PRIVILEGE_ENABLED}}};
        if (LookupPrivilegeValueA(NULL, "SeLockMemoryPrivilege", &tp.Privileges[0].Luid))
            AdjustTokenPrivileges(token, FALSE, &tp, 0, NULL, NULL);
        int ok = GetLastError() == ERROR_SUCCESS;
        CloseHandle(token);
        SIZE_T minimo = GetLargePageMinimum();
        if (ok && minimo) {
            size_t tam = (*bytes + minimo - 1) / minimo * minimo;
            void *p = VirtualAlloc(NULL, tam, MEM_COMMIT | MEM_RESERVE | MEM_LARGE_PAGES, PAGE_READWRITE);
            if (p) { *bytes = tam; *grande = 1; return p; }
        }
    }
    return aloca(*bytes);
#else
    void *p = aloca(*bytes);
#ifdef MADV_HUGEPAGE
    if (p && madvise(p, *bytes, MADV_HUGEPAGE) == 0) *grande = 1;
#endif
    return p;
#endif
}

static uint64_t semente = 88172645463325252ULL;
static uint64_t aleatorio(void) {
    semente ^= semente << 13;
    semente ^= semente >> 7;
    semente ^= semente << 17;
    return semente;
}

static void embaralha(size_t *v, size_t n) {
    for (size_t i = n - 1; i > 0; i--) {
        size_t j = aleatorio() % (i + 1);
        size_t t = v[i]; v[i] = v[j]; v[j] = t;
    }
}

/* ------------------------------------------------------------------ processador */

/* Tamanhos de L1 (dados), L2 e L3 em bytes; 0 quando desconhecido. */
EXPORTA int mt_caches(int64_t out[3]) {
    out[0] = out[1] = out[2] = 0;
#ifdef _WIN32
    DWORD tam = 0;
    GetLogicalProcessorInformation(NULL, &tam);
    SYSTEM_LOGICAL_PROCESSOR_INFORMATION *info = malloc(tam);
    if (!info || !GetLogicalProcessorInformation(info, &tam)) { free(info); return 0; }
    for (DWORD i = 0; i < tam / sizeof(*info); i++) {
        if (info[i].Relationship != RelationCache) continue;
        CACHE_DESCRIPTOR c = info[i].Cache;
        if (c.Level == 1 && c.Type != CacheData && c.Type != CacheUnified) continue;
        if (c.Level >= 1 && c.Level <= 3 && (int64_t)c.Size > out[c.Level - 1]) out[c.Level - 1] = c.Size;
    }
    free(info);
#else
    out[0] = sysconf(_SC_LEVEL1_DCACHE_SIZE);
    out[1] = sysconf(_SC_LEVEL2_CACHE_SIZE);
    out[2] = sysconf(_SC_LEVEL3_CACHE_SIZE);
    for (int i = 0; i < 3; i++) if (out[i] < 0) out[i] = 0;
#endif
    return 1;
}

/*
 * Processadores lógicos em ordem: primeiro um por núcleo físico, depois os threads extras do
 * Hyper-Threading/SMT. Devolve o total e, em `fisicos`, quantos são núcleos físicos.
 */
static int nucleos(int cpus[MAX_FIOS], int *fisicos) {
    int n = 0;
#ifdef _WIN32
    DWORD tam = 0;
    GetLogicalProcessorInformation(NULL, &tam);
    SYSTEM_LOGICAL_PROCESSOR_INFORMATION *info = malloc(tam);
    ULONG_PTR extras = 0;
    if (info && GetLogicalProcessorInformation(info, &tam)) {
        for (DWORD i = 0; i < tam / sizeof(*info) && n < MAX_FIOS; i++) {
            ULONG_PTR mascara = info[i].ProcessorMask;
            if (info[i].Relationship != RelationProcessorCore || !mascara) continue;
            int bit = 0;
            while (!(mascara & ((ULONG_PTR)1 << bit))) bit++;
            cpus[n++] = bit;
            extras |= mascara & ~((ULONG_PTR)1 << bit);
        }
    }
    free(info);
    *fisicos = n;
    for (int bit = 0; bit < 64 && n < MAX_FIOS; bit++)
        if (extras & ((ULONG_PTR)1 << bit)) cpus[n++] = bit;
#else
    long total = sysconf(_SC_NPROCESSORS_ONLN);
    for (long i = 0; i < total && n < MAX_FIOS; i++) cpus[n++] = (int)i;
    *fisicos = n;
#endif
    if (!n) { cpus[n++] = 0; *fisicos = 1; }
    return n;
}

/* Núcleos físicos; com `logicos` = 1, todos os processadores lógicos. */
EXPORTA int mt_nucleos(int logicos) {
    int cpus[MAX_FIOS], fisicos;
    int total = nucleos(cpus, &fisicos);
    return logicos ? total : fisicos;
}

/* 3 = AVX-512, 2 = AVX2, 1 = SSE2 */
EXPORTA int mt_isa(void) {
    __builtin_cpu_init();
    if (__builtin_cpu_supports("avx512f")) return 3;
    if (__builtin_cpu_supports("avx2")) return 2;
    return 1;
}

/* ------------------------------------------------------------------ latência */

#define LINHA 64
#define LINHAS_PAGINA (PAGINA / LINHA)
#define JANELA 2048 /* páginas (8 MB) sorteadas juntas: cabem na TLB */

/*
 * Latência média de um acesso dependente (ns) num bloco de `bytes`.
 *
 * Todas as linhas de 64 B do bloco são visitadas, mas em 64 voltas: em cada volta, uma linha de
 * cada página, com as páginas em ordem aleatória. Assim duas visitas à mesma página ficam sempre
 * separadas por uma volta inteira. Visitar várias linhas da mesma página em seguida deixaria o
 * prefetcher de região (forte nos Ryzen) trazer a página inteira de uma vez, e a latência sairia
 * baixa demais. As páginas são sorteadas em janelas de 8 MB para a tradução de endereços (TLB)
 * não inflar o resultado.
 */
EXPORTA double mt_latencia(int64_t bytes, double segundos, int *usou_grande) {
    if (bytes < PAGINA) bytes = PAGINA;
    size_t paginas = (size_t)bytes / PAGINA, n = paginas * LINHAS_PAGINA;
    size_t reservado = paginas * PAGINA;
    int grande = 0;
    char *mem = aloca_grande(&reservado, &grande);
    if (usou_grande) *usou_grande = grande;
    size_t *ordem = malloc(paginas * sizeof(size_t));
    unsigned char *inicio = malloc(paginas);
    if (!mem || !ordem || !inicio) { libera(mem, reservado); free(ordem); free(inicio); return -1; }

    size_t janelas = (paginas + JANELA - 1) / JANELA;
    size_t *ordem_jan = malloc(janelas * sizeof(size_t)), *por_janela = malloc(paginas * sizeof(size_t));
    if (!ordem_jan || !por_janela) {
        libera(mem, reservado); free(ordem); free(inicio); free(ordem_jan); free(por_janela); return -1;
    }
    for (size_t i = 0; i < paginas; i++) { por_janela[i] = i; inicio[i] = aleatorio() % LINHAS_PAGINA; }
    for (size_t j = 0; j < janelas; j++) ordem_jan[j] = j;
    embaralha(ordem_jan, janelas);
    size_t k = 0;
    for (size_t j = 0; j < janelas; j++) {
        size_t ini = ordem_jan[j] * JANELA, fim = ini + JANELA < paginas ? ini + JANELA : paginas;
        embaralha(por_janela + ini, fim - ini);
        for (size_t i = ini; i < fim; i++) ordem[k++] = por_janela[i];
    }
    free(ordem_jan);
    free(por_janela);

    void **primeiro = NULL, **anterior = NULL;
    for (size_t volta = 0; volta < LINHAS_PAGINA; volta++) {
        for (size_t i = 0; i < paginas; i++) {
            size_t pag = ordem[i], linha = (inicio[pag] + volta * 23) % LINHAS_PAGINA; /* 23: salta linhas vizinhas */
            void **no = (void **)(mem + pag * PAGINA + linha * LINHA);
            if (anterior) *anterior = (void *)no; else primeiro = no;
            anterior = no;
        }
    }
    *anterior = (void *)primeiro;
    free(ordem);
    free(inicio);

    void **p = primeiro;
    for (size_t i = 0; i < n; i++) p = (void **)*p; /* aquece cache e TLB */

    /* Melhor de 3 rodadas: descarta interrupções do sistema no meio da medição. */
    double melhor = 1e30;
    int64_t acessos = 1 << 16;
    for (int rodada = 0; rodada < 3; rodada++) {
        double t;
        for (;;) {
            double t0 = agora_s();
            for (int64_t i = 0; i < acessos; i += 16) {
                p = (void **)*p; p = (void **)*p; p = (void **)*p; p = (void **)*p;
                p = (void **)*p; p = (void **)*p; p = (void **)*p; p = (void **)*p;
                p = (void **)*p; p = (void **)*p; p = (void **)*p; p = (void **)*p;
                p = (void **)*p; p = (void **)*p; p = (void **)*p; p = (void **)*p;
            }
            t = agora_s() - t0;
            if (t >= segundos / 3 || acessos > ((int64_t)1 << 40)) break;
            acessos = t > 0.001 ? (int64_t)(acessos * (segundos / 3) / t) + 16 : acessos * 16;
        }
        double ns = t * 1e9 / (double)acessos;
        if (ns < melhor) melhor = ns;
    }
    void *volatile sumidouro = p;
    (void)sumidouro;
    libera(mem, reservado);
    return melhor;
}

/* ------------------------------------------------------------------ núcleos de banda */

__attribute__((target("avx512f"))) static uint64_t ler_avx512(const char *p, size_t n) {
    __m512i a = _mm512_setzero_si512(), b = a, c = a, d = a;
    for (size_t i = 0; i < n; i += 512) {
        a = _mm512_xor_si512(a, _mm512_load_si512(p + i));
        b = _mm512_xor_si512(b, _mm512_load_si512(p + i + 64));
        c = _mm512_xor_si512(c, _mm512_load_si512(p + i + 128));
        d = _mm512_xor_si512(d, _mm512_load_si512(p + i + 192));
        a = _mm512_xor_si512(a, _mm512_load_si512(p + i + 256));
        b = _mm512_xor_si512(b, _mm512_load_si512(p + i + 320));
        c = _mm512_xor_si512(c, _mm512_load_si512(p + i + 384));
        d = _mm512_xor_si512(d, _mm512_load_si512(p + i + 448));
    }
    uint64_t v[8];
    _mm512_storeu_si512(v, _mm512_xor_si512(_mm512_xor_si512(a, b), _mm512_xor_si512(c, d)));
    return v[0] ^ v[7];
}

__attribute__((target("avx512f"))) static void escrever_avx512(char *p, size_t n, int valor) {
    __m512i v = _mm512_set1_epi32(valor);
    for (size_t i = 0; i < n; i += 256) {
        _mm512_stream_si512((void *)(p + i), v);
        _mm512_stream_si512((void *)(p + i + 64), v);
        _mm512_stream_si512((void *)(p + i + 128), v);
        _mm512_stream_si512((void *)(p + i + 192), v);
    }
    _mm_sfence();
}

__attribute__((target("avx512f"))) static void copiar_avx512(char *dst, const char *src, size_t n) {
    for (size_t i = 0; i < n; i += 256) {
        __m512i a = _mm512_load_si512(src + i), b = _mm512_load_si512(src + i + 64);
        __m512i c = _mm512_load_si512(src + i + 128), d = _mm512_load_si512(src + i + 192);
        _mm512_stream_si512((void *)(dst + i), a);
        _mm512_stream_si512((void *)(dst + i + 64), b);
        _mm512_stream_si512((void *)(dst + i + 128), c);
        _mm512_stream_si512((void *)(dst + i + 192), d);
    }
    _mm_sfence();
}

__attribute__((target("avx2"))) static uint64_t ler_avx2(const char *p, size_t n) {
    __m256i a = _mm256_setzero_si256(), b = a, c = a, d = a;
    for (size_t i = 0; i < n; i += 256) {
        a = _mm256_xor_si256(a, _mm256_load_si256((const __m256i *)(p + i)));
        b = _mm256_xor_si256(b, _mm256_load_si256((const __m256i *)(p + i + 32)));
        c = _mm256_xor_si256(c, _mm256_load_si256((const __m256i *)(p + i + 64)));
        d = _mm256_xor_si256(d, _mm256_load_si256((const __m256i *)(p + i + 96)));
        a = _mm256_xor_si256(a, _mm256_load_si256((const __m256i *)(p + i + 128)));
        b = _mm256_xor_si256(b, _mm256_load_si256((const __m256i *)(p + i + 160)));
        c = _mm256_xor_si256(c, _mm256_load_si256((const __m256i *)(p + i + 192)));
        d = _mm256_xor_si256(d, _mm256_load_si256((const __m256i *)(p + i + 224)));
    }
    uint64_t v[4];
    _mm256_storeu_si256((__m256i *)v, _mm256_xor_si256(_mm256_xor_si256(a, b), _mm256_xor_si256(c, d)));
    return v[0] ^ v[3];
}

__attribute__((target("avx2"))) static void escrever_avx2(char *p, size_t n, int valor) {
    __m256i v = _mm256_set1_epi32(valor);
    for (size_t i = 0; i < n; i += 128) {
        _mm256_stream_si256((__m256i *)(p + i), v);
        _mm256_stream_si256((__m256i *)(p + i + 32), v);
        _mm256_stream_si256((__m256i *)(p + i + 64), v);
        _mm256_stream_si256((__m256i *)(p + i + 96), v);
    }
    _mm_sfence();
}

__attribute__((target("avx2"))) static void copiar_avx2(char *dst, const char *src, size_t n) {
    for (size_t i = 0; i < n; i += 128) {
        __m256i a = _mm256_load_si256((const __m256i *)(src + i));
        __m256i b = _mm256_load_si256((const __m256i *)(src + i + 32));
        __m256i c = _mm256_load_si256((const __m256i *)(src + i + 64));
        __m256i d = _mm256_load_si256((const __m256i *)(src + i + 96));
        _mm256_stream_si256((__m256i *)(dst + i), a);
        _mm256_stream_si256((__m256i *)(dst + i + 32), b);
        _mm256_stream_si256((__m256i *)(dst + i + 64), c);
        _mm256_stream_si256((__m256i *)(dst + i + 96), d);
    }
    _mm_sfence();
}

static uint64_t ler_sse2(const char *p, size_t n) {
    __m128i a = _mm_setzero_si128(), b = a, c = a, d = a;
    for (size_t i = 0; i < n; i += 128) {
        a = _mm_xor_si128(a, _mm_load_si128((const __m128i *)(p + i)));
        b = _mm_xor_si128(b, _mm_load_si128((const __m128i *)(p + i + 16)));
        c = _mm_xor_si128(c, _mm_load_si128((const __m128i *)(p + i + 32)));
        d = _mm_xor_si128(d, _mm_load_si128((const __m128i *)(p + i + 48)));
        a = _mm_xor_si128(a, _mm_load_si128((const __m128i *)(p + i + 64)));
        b = _mm_xor_si128(b, _mm_load_si128((const __m128i *)(p + i + 80)));
        c = _mm_xor_si128(c, _mm_load_si128((const __m128i *)(p + i + 96)));
        d = _mm_xor_si128(d, _mm_load_si128((const __m128i *)(p + i + 112)));
    }
    uint64_t v[2];
    _mm_storeu_si128((__m128i *)v, _mm_xor_si128(_mm_xor_si128(a, b), _mm_xor_si128(c, d)));
    return v[0] ^ v[1];
}

static void escrever_sse2(char *p, size_t n, int valor) {
    __m128i v = _mm_set1_epi32(valor);
    for (size_t i = 0; i < n; i += 64) {
        _mm_stream_si128((__m128i *)(p + i), v);
        _mm_stream_si128((__m128i *)(p + i + 16), v);
        _mm_stream_si128((__m128i *)(p + i + 32), v);
        _mm_stream_si128((__m128i *)(p + i + 48), v);
    }
    _mm_sfence();
}

static void copiar_sse2(char *dst, const char *src, size_t n) {
    for (size_t i = 0; i < n; i += 64) {
        __m128i a = _mm_load_si128((const __m128i *)(src + i));
        __m128i b = _mm_load_si128((const __m128i *)(src + i + 16));
        __m128i c = _mm_load_si128((const __m128i *)(src + i + 32));
        __m128i d = _mm_load_si128((const __m128i *)(src + i + 48));
        _mm_stream_si128((__m128i *)(dst + i), a);
        _mm_stream_si128((__m128i *)(dst + i + 16), b);
        _mm_stream_si128((__m128i *)(dst + i + 32), c);
        _mm_stream_si128((__m128i *)(dst + i + 48), d);
    }
    _mm_sfence();
}

/* ------------------------------------------------------------------ banda */

typedef struct {
    int tipo; /* 0 leitura, 1 escrita, 2 cópia */
    int isa;
    int cpu;
    char *buf;
    size_t bytes;
    int repeticoes;
    volatile int *largada;
    volatile int *prontos;
    double fim;
    uint64_t soma;
} Tarefa;

static void executa(Tarefa *t) {
#ifdef _WIN32
    SetThreadAffinityMask(GetCurrentThread(), (DWORD_PTR)1 << t->cpu);
#else
    cpu_set_t conjunto;
    CPU_ZERO(&conjunto);
    CPU_SET(t->cpu, &conjunto);
    pthread_setaffinity_np(pthread_self(), sizeof(conjunto), &conjunto);
#endif
    memset(t->buf, 1, t->bytes); /* toca a memória no núcleo que vai usá-la */
    __sync_fetch_and_add(t->prontos, 1);
    while (!*t->largada) { }
    size_t metade = t->bytes / 2;
    for (int r = 0; r < t->repeticoes; r++) {
        switch (t->tipo * 4 + t->isa) {
        case 3: t->soma += ler_avx512(t->buf, t->bytes); break;
        case 2: t->soma += ler_avx2(t->buf, t->bytes); break;
        case 1: t->soma += ler_sse2(t->buf, t->bytes); break;
        case 7: escrever_avx512(t->buf, t->bytes, r); break;
        case 6: escrever_avx2(t->buf, t->bytes, r); break;
        case 5: escrever_sse2(t->buf, t->bytes, r); break;
        case 11: copiar_avx512(t->buf + metade, t->buf, metade); break;
        case 10: copiar_avx2(t->buf + metade, t->buf, metade); break;
        case 9: copiar_sse2(t->buf + metade, t->buf, metade); break;
        }
    }
    t->fim = agora_s();
}

#ifdef _WIN32
static DWORD WINAPI fio(LPVOID arg) { executa((Tarefa *)arg); return 0; }
#else
static void *fio(void *arg) { executa((Tarefa *)arg); return NULL; }
#endif

/*
 * Banda em bytes por segundo. `threads` <= 0 usa um por núcleo físico (acima disso entram os
 * threads de SMT); `isa` 0 escolhe a
 * melhor instrução disponível.
 */
EXPORTA double mt_banda(int tipo, int64_t bytes_total, int threads, int repeticoes, int isa) {
    int cpus[MAX_FIOS], fisicos;
    int disponiveis = nucleos(cpus, &fisicos);
    if (threads <= 0) threads = fisicos;
    if (threads > disponiveis) threads = disponiveis;
    if (isa <= 0 || isa > mt_isa()) isa = mt_isa();
    size_t por_fio = ((size_t)bytes_total / threads) & ~(size_t)(PAGINA - 1);
    Tarefa tarefas[MAX_FIOS];
    volatile int largada = 0, prontos = 0;
#ifdef _WIN32
    HANDLE alcas[MAX_FIOS];
#else
    pthread_t alcas[MAX_FIOS];
#endif
    for (int i = 0; i < threads; i++) {
        tarefas[i] = (Tarefa){tipo, isa, cpus[i], aloca(por_fio), por_fio, repeticoes, &largada, &prontos, 0, 0};
        if (!tarefas[i].buf) { for (int k = 0; k < i; k++) libera(tarefas[k].buf, por_fio); return -1; }
    }
    for (int i = 0; i < threads; i++) {
#ifdef _WIN32
        alcas[i] = CreateThread(NULL, 0, fio, &tarefas[i], 0, NULL);
#else
        pthread_create(&alcas[i], NULL, fio, &tarefas[i]);
#endif
    }
    while (prontos < threads) { }
    double t0 = agora_s();
    largada = 1;
    double fim = t0;
    for (int i = 0; i < threads; i++) {
#ifdef _WIN32
        WaitForSingleObject(alcas[i], INFINITE);
        CloseHandle(alcas[i]);
#else
        pthread_join(alcas[i], NULL);
#endif
        if (tarefas[i].fim > fim) fim = tarefas[i].fim;
    }
    uint64_t soma = 0;
    for (int i = 0; i < threads; i++) { soma ^= tarefas[i].soma; libera(tarefas[i].buf, por_fio); }
    volatile uint64_t sumidouro = soma;
    (void)sumidouro;
    /* Na cópia, metade do bloco é lida e a outra metade escrita: conta os dois (como o STREAM). */
    double bytes = (double)por_fio * threads * repeticoes;
    return fim > t0 ? bytes / (fim - t0) : -1;
}

/* teste de disco (mesma DLL) */
#include "discoteste.c"

/* benchmarks e estresse (mesma DLL) */
#include "bench.c"
