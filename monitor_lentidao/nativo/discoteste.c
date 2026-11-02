/* Teste de velocidade de disco no estilo do CrystalDiskMark (incluído por memteste.c, vai na mesma DLL).
 *
 * Cada thread abre o próprio handle do arquivo SEM o cache do sistema e faz E/S síncrona; N threads
 * equivalem a N requisições na fila do disco (é a "profundidade de fila" que os SSDs NVMe precisam para
 * mostrar a velocidade máxima). O tempo é medido só depois que todas as threads estão prontas.
 */

#ifdef _WIN32
typedef HANDLE Arq;
#define ARQ_INVALIDO INVALID_HANDLE_VALUE
#else
#include <fcntl.h>
#include <sys/stat.h>
#include <wchar.h>
typedef int Arq;
#define ARQ_INVALIDO (-1)
#endif

static Arq dt_abrir(const wchar_t *caminho, int criar) {
#ifdef _WIN32
    /* Sem cache (NO_BUFFERING). Sem WRITE_THROUGH, como o CrystalDiskMark: o cache do próprio SSD vale. */
    return CreateFileW(caminho, GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                       NULL, criar ? CREATE_ALWAYS : OPEN_EXISTING, FILE_FLAG_NO_BUFFERING, NULL);
#else
    char c[4096];
    if (wcstombs(c, caminho, sizeof c) == (size_t)-1) return ARQ_INVALIDO;
    int flags = O_RDWR | (criar ? O_CREAT | O_TRUNC : 0);
#ifdef O_DIRECT
    flags |= O_DIRECT;
#endif
    return open(c, flags, 0600);
#endif
}

static void dt_fechar(Arq a) {
#ifdef _WIN32
    CloseHandle(a);
#else
    close(a);
#endif
}

static int dt_io(Arq a, void *buf, uint32_t n, uint64_t pos, int escrever) {
#ifdef _WIN32
    OVERLAPPED ov;
    memset(&ov, 0, sizeof ov);
    ov.Offset = (DWORD)pos;
    ov.OffsetHigh = (DWORD)(pos >> 32);
    DWORD feito = 0;
    BOOL ok = escrever ? WriteFile(a, buf, n, &feito, &ov) : ReadFile(a, buf, n, &feito, &ov);
    return ok && feito == n;
#else
    ssize_t r = escrever ? pwrite(a, buf, n, (off_t)pos) : pread(a, buf, n, (off_t)pos);
    return r == (ssize_t)n;
#endif
}

static void *dt_buffer(size_t n) {
#ifdef _WIN32
    return VirtualAlloc(NULL, n, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
#else
    void *p = NULL;
    return posix_memalign(&p, 4096, n) == 0 ? p : NULL;
#endif
}

static void dt_libera(void *p) {
#ifdef _WIN32
    VirtualFree(p, 0, MEM_RELEASE);
#else
    free(p);
#endif
}

static void dt_aleatorio(uint8_t *p, size_t n, uint64_t s) {
    /* dados que não comprimem: alguns controladores comprimem e "trapaceiam" com zeros */
    for (size_t i = 0; i + 8 <= n; i += 8) {
        s ^= s << 13; s ^= s >> 7; s ^= s << 17;
        memcpy(p + i, &s, 8);
    }
}

/* Cria o arquivo de teste e grava ele inteiro (blocos de 1 MiB), do mesmo jeito que o CrystalDiskMark:
 * tira a compressão do NTFS (num arquivo comprimido o Windows ignora o "sem cache" e o teste mede a RAM),
 * reserva o tamanho de uma vez e grava tudo com dados aleatórios. Devolve 1 se deu certo. */
EXPORTA int dt_preparar(const wchar_t *caminho, int64_t bytes) {
    const uint32_t bloco = 1 << 20;
#ifdef _WIN32
    HANDLE a = CreateFileW(caminho, GENERIC_READ | GENERIC_WRITE, 0, NULL, CREATE_ALWAYS,
                           FILE_ATTRIBUTE_NORMAL | FILE_FLAG_NO_BUFFERING | FILE_FLAG_SEQUENTIAL_SCAN, NULL);
    if (a == INVALID_HANDLE_VALUE) return 0;
    USHORT sem = 0; /* COMPRESSION_FORMAT_NONE */
    DWORD ret = 0;
    DeviceIoControl(a, FSCTL_SET_COMPRESSION, &sem, sizeof sem, NULL, 0, &ret, NULL);
    LARGE_INTEGER tam, zero;
    tam.QuadPart = bytes;
    zero.QuadPart = 0;
    SetFilePointerEx(a, tam, NULL, FILE_BEGIN);
    SetEndOfFile(a);
    SetFilePointerEx(a, zero, NULL, FILE_BEGIN);
#else
    Arq a = dt_abrir(caminho, 1);
    if (a == ARQ_INVALIDO) return 0;
#endif
    uint8_t *buf = dt_buffer(bloco);
    int ok = buf != NULL;
    if (ok) dt_aleatorio(buf, bloco, 0x9E3779B97F4A7C15ULL);
    for (int64_t pos = 0; ok && pos < bytes; pos += bloco) {
#ifdef _WIN32
        DWORD feito = 0;
        ok = WriteFile(a, buf, bloco, &feito, NULL) && feito == bloco;
#else
        ok = dt_io(a, buf, bloco, (uint64_t)pos, 1);
#endif
    }
#ifdef _WIN32
    FlushFileBuffers(a);
#endif
    if (buf) dt_libera(buf);
    dt_fechar(a);
    return ok;
}

/* Confere se o teste vai medir o disco de verdade.
 * 0 = tudo certo; 1 = arquivo comprimido pelo NTFS; 2 = conteúdo não ficou gravado; -1 = erro ao abrir. */
EXPORTA int dt_verificar(const wchar_t *caminho, int64_t bytes) {
#ifdef _WIN32
    DWORD attr = GetFileAttributesW(caminho);
    if (attr == INVALID_FILE_ATTRIBUTES) return -1;
    if (attr & (FILE_ATTRIBUTE_COMPRESSED | FILE_ATTRIBUTE_SPARSE_FILE)) return 1;
#endif
    Arq a = dt_abrir(caminho, 0);
    if (a == ARQ_INVALIDO) return -1;
    const uint32_t bloco = 1 << 20;
    uint8_t *buf = dt_buffer(bloco);
    int r = buf ? 0 : -1;
    int64_t posicoes[3] = {0, (bytes / 2) / bloco * bloco, bytes - bloco};
    for (int i = 0; i < 3 && r == 0; i++) {
        if (!dt_io(a, buf, bloco, (uint64_t)posicoes[i], 0)) { r = -1; break; }
        int tudo_zero = 1;
        for (uint32_t k = 0; k < bloco; k += 64) if (buf[k]) { tudo_zero = 0; break; }
        if (tudo_zero) r = 2;
    }
    if (buf) dt_libera(buf);
    dt_fechar(a);
    return r;
}

typedef struct {
    const wchar_t *caminho;
    int64_t bytes, ops;
    uint32_t bloco;
    int aleatorio, escrever, indice, fios, fila, erro;
    double fim;
    volatile int *largada;
} DtFio;

static void dt_espera_largada(DtFio *f) {
    while (!*f->largada) {
#ifdef _WIN32
        Sleep(0);
#else
        sched_yield();
#endif
    }
}

/* próximo bloco: sequencial anda de bloco em bloco a partir da faixa da thread; aleatório sorteia */
static uint64_t dt_proximo(DtFio *f, uint64_t *s, uint64_t *cursor, uint64_t blocos) {
    if (f->aleatorio) { *s ^= *s << 13; *s ^= *s >> 7; *s ^= *s << 17; return *s % blocos; }
    return (*cursor)++ % blocos;
}

#ifdef _WIN32
/* Windows: uma thread mantém `fila` requisições em andamento ao mesmo tempo (E/S assíncrona com
 * porta de conclusão), exatamente como o CrystalDiskMark faz no "Q8T1"/"Q32T1". */
static void dt_executa(DtFio *f) {
    HANDLE a = CreateFileW(f->caminho, GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                           NULL, OPEN_EXISTING, FILE_FLAG_NO_BUFFERING | FILE_FLAG_OVERLAPPED, NULL);
    if (a == INVALID_HANDLE_VALUE) { f->erro = 1; return; }
    HANDLE porta = CreateIoCompletionPort(a, NULL, 0, 1);
    int q = f->fila < 1 ? 1 : f->fila > 64 ? 64 : f->fila;
    uint8_t *buf = dt_buffer((size_t)f->bloco * q);
    OVERLAPPED ov[64];
    if (!porta || !buf) { f->erro = 1; goto fim; }
    dt_aleatorio(buf, (size_t)f->bloco * q, 0xA5A5A5A5ULL + (uint64_t)f->indice * 7919);
    uint64_t blocos = (uint64_t)(f->bytes / f->bloco);
    uint64_t s = 0x2545F4914F6CDD1DULL ^ ((uint64_t)f->indice << 32);
    uint64_t cursor = blocos / (uint64_t)f->fios * (uint64_t)f->indice;
    int pendentes = 0;
    dt_espera_largada(f);

    #define DT_ENVIA(i) do { \
        uint64_t pos_ = dt_proximo(f, &s, &cursor, blocos) * f->bloco; \
        memset(&ov[i], 0, sizeof ov[i]); ov[i].Offset = (DWORD)pos_; ov[i].OffsetHigh = (DWORD)(pos_ >> 32); \
        BOOL ok_ = f->escrever ? WriteFile(a, buf + (size_t)(i) * f->bloco, f->bloco, NULL, &ov[i]) \
                               : ReadFile(a, buf + (size_t)(i) * f->bloco, f->bloco, NULL, &ov[i]); \
        if (!ok_ && GetLastError() != ERROR_IO_PENDING) { f->erro = 1; } else pendentes++; \
    } while (0)

    for (int i = 0; i < q && !f->erro; i++) DT_ENVIA(i);
    while (pendentes > 0) {
        DWORD n = 0; ULONG_PTR chave; OVERLAPPED *o = NULL;
        BOOL ok = GetQueuedCompletionStatus(porta, &n, &chave, &o, 5000);
        if (!o) {                              /* travou: cancela e espera as pendentes antes de liberar a memória */
            f->erro = 1;
            CancelIoEx(a, NULL);
            while (pendentes > 0 && (GetQueuedCompletionStatus(porta, &n, &chave, &o, 10000) || o)) pendentes--;
            break;
        }
        pendentes--;
        if (!ok || n != f->bloco) { f->erro = 1; continue; }
        if (agora_s() < f->fim) { f->ops++; if (!f->erro) DT_ENVIA((int)(o - ov)); }
    }
    #undef DT_ENVIA
fim:
    if (buf) dt_libera(buf);
    if (porta) CloseHandle(porta);
    CloseHandle(a);
}
#else
/* Fora do Windows (só para testes): cada thread faz uma requisição por vez; a fila vira mais threads. */
static void dt_executa(DtFio *f) {
    Arq a = dt_abrir(f->caminho, 0);
    uint8_t *buf = dt_buffer(f->bloco);
    if (a == ARQ_INVALIDO || !buf) { f->erro = 1; if (buf) dt_libera(buf); if (a != ARQ_INVALIDO) dt_fechar(a); return; }
    dt_aleatorio(buf, f->bloco, 0xA5A5A5A5ULL + (uint64_t)f->indice * 7919);
    uint64_t blocos = (uint64_t)(f->bytes / f->bloco);
    uint64_t s = 0x2545F4914F6CDD1DULL ^ ((uint64_t)f->indice << 32);
    uint64_t cursor = blocos / (uint64_t)f->fios * (uint64_t)f->indice;
    dt_espera_largada(f);
    while (agora_s() < f->fim) {
        for (int k = 0; k < 16; k++) {
            if (!dt_io(a, buf, f->bloco, dt_proximo(f, &s, &cursor, blocos) * f->bloco, f->escrever)) { f->erro = 1; goto fim; }
            f->ops++;
        }
    }
fim:
    dt_libera(buf);
    dt_fechar(a);
}
#endif

#ifdef _WIN32
static DWORD WINAPI dt_fio(LPVOID p) { dt_executa((DtFio *)p); return 0; }
#else
static void *dt_fio(void *p) { dt_executa((DtFio *)p); return NULL; }
#endif

/* Mede durante `segundos` com `fios` threads, cada uma com `fila` requisições em andamento.
 * Devolve bytes por segundo (negativo se houve erro). */
EXPORTA double dt_medir(const wchar_t *caminho, int64_t bytes, int bloco, int aleatorio, int escrever,
                        int fila, int fios, double segundos) {
#ifndef _WIN32
    fios *= fila;  /* sem E/S assíncrona: a fila vira threads */
    fila = 1;
#endif
    if (fios < 1) fios = 1;
    if (fios > 64) fios = 64;
    DtFio f[64];
    volatile int largada = 0;
    double ini = agora_s() + 0.2;  /* tempo para as threads abrirem o arquivo */
    for (int i = 0; i < fios; i++) {
        f[i] = (DtFio){caminho, bytes, 0, (uint32_t)bloco, aleatorio, escrever, i, fios, fila, 0, ini + segundos, &largada};
    }
#ifdef _WIN32
    HANDLE h[64];
    for (int i = 0; i < fios; i++) h[i] = CreateThread(NULL, 0, dt_fio, &f[i], 0, NULL);
#else
    pthread_t h[64];
    for (int i = 0; i < fios; i++) pthread_create(&h[i], NULL, dt_fio, &f[i]);
#endif
    while (agora_s() < ini) {
#ifdef _WIN32
        Sleep(1);
#else
        usleep(1000);
#endif
    }
    double t0 = agora_s();
    largada = 1;
#ifdef _WIN32
    WaitForMultipleObjects((DWORD)fios, h, TRUE, INFINITE);
    for (int i = 0; i < fios; i++) CloseHandle(h[i]);
#else
    for (int i = 0; i < fios; i++) pthread_join(h[i], NULL);
#endif
    double dt = (ini + segundos) - t0;  /* só conta o que terminou dentro do tempo */
    int64_t ops = 0;
    int erro = 0;
    for (int i = 0; i < fios; i++) { ops += f[i].ops; erro |= f[i].erro; }
    if (erro && !ops) return -1.0;
    return dt > 0 ? (double)ops * bloco / dt : 0.0;
}

/* ------------------------------------------------------------------ carregamento de jogo
 * Como um jogo carrega um mapa: um pacote grande lido em sequência e muitos arquivos pequenos (texturas, sons,
 * scripts) abertos por várias threads. Tudo sem o cache do Windows, para medir o disco e não a RAM. */

static Arq dc_abrir_leitura(const wchar_t *caminho) {
#ifdef _WIN32
    return CreateFileW(caminho, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_DELETE, NULL, OPEN_EXISTING,
                       FILE_FLAG_NO_BUFFERING | FILE_FLAG_SEQUENTIAL_SCAN, NULL);
#else
    char c[4096];
    if (wcstombs(c, caminho, sizeof c) == (size_t)-1) return ARQ_INVALIDO;
    int a = -1;
#ifdef O_DIRECT
    a = open(c, O_RDONLY | O_DIRECT);
#endif
    if (a < 0) a = open(c, O_RDONLY);  /* tmpfs e afins não aceitam O_DIRECT */
    return a;
#endif
}

/* Lê o arquivo inteiro em blocos de 4 MiB. Devolve os segundos (negativo se deu erro). */
EXPORTA double dc_ler_pacote(const wchar_t *caminho, int64_t bytes) {
    const uint32_t bloco = 4u << 20;
    uint8_t *buf = dt_buffer(bloco);
    if (!buf) return -1.0;
    Arq a = dc_abrir_leitura(caminho);
    if (a == ARQ_INVALIDO) { dt_libera(buf); return -1.0; }
    double t0 = agora_s();
    int ok = 1;
    for (int64_t pos = 0; ok && pos < bytes; pos += bloco) {
        uint32_t n = (uint32_t)((bytes - pos) < bloco ? (bytes - pos) : bloco);
        ok = dt_io(a, buf, n, (uint64_t)pos, 0);
    }
    double dt = agora_s() - t0;
    dt_fechar(a);
    dt_libera(buf);
    return ok ? dt : -1.0;
}

typedef struct {
    const wchar_t *const *nomes;
    int n, passo, inicio;
    uint32_t bytes;
    int erro;
} DcFio;

static void dc_executa(DcFio *f) {
    uint8_t *buf = dt_buffer(f->bytes);
    if (!buf) { f->erro = 1; return; }
    for (int i = f->inicio; i < f->n && !f->erro; i += f->passo) {
        Arq a = dc_abrir_leitura(f->nomes[i]);
        if (a == ARQ_INVALIDO) { f->erro = 1; break; }
        if (!dt_io(a, buf, f->bytes, 0, 0)) f->erro = 1;
        dt_fechar(a);
    }
    dt_libera(buf);
}
#ifdef _WIN32
static DWORD WINAPI dc_fio(LPVOID p) { dc_executa((DcFio *)p); return 0; }
#else
static void *dc_fio(void *p) { dc_executa((DcFio *)p); return NULL; }
#endif

/* Abre e lê `n` arquivos de `bytes` cada (múltiplo de 4096) com `fios` threads. Devolve os segundos. */
EXPORTA double dc_ler_arquivos(const wchar_t *const *nomes, int n, int bytes, int fios) {
    if (fios < 1) fios = 1;
    if (fios > 64) fios = 64;
    DcFio f[64];
    for (int i = 0; i < fios; i++) f[i] = (DcFio){nomes, n, fios, i, (uint32_t)bytes, 0};
    double t0 = agora_s();
#ifdef _WIN32
    HANDLE h[64];
    for (int i = 0; i < fios; i++) h[i] = CreateThread(NULL, 0, dc_fio, &f[i], 0, NULL);
    WaitForMultipleObjects((DWORD)fios, h, TRUE, INFINITE);
    for (int i = 0; i < fios; i++) CloseHandle(h[i]);
#else
    pthread_t h[64];
    for (int i = 0; i < fios; i++) pthread_create(&h[i], NULL, dc_fio, &f[i]);
    for (int i = 0; i < fios; i++) pthread_join(h[i], NULL);
#endif
    double dt = agora_s() - t0;
    for (int i = 0; i < fios; i++) if (f[i].erro) return -1.0;
    return dt;
}
