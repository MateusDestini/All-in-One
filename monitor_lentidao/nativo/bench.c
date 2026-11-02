/*
 * Benchmarks e testes de estresse do Monitor de Lentidão (mesma DLL do teste de memória).
 *
 *  - bc_render: path tracer de CPU no modelo do Cinebench (imagem em blocos distribuídos entre threads,
 *    do centro para fora). Cena e sementes fixas: o volume de trabalho é idêntico em toda execução.
 *  - gs_rodar: "CPU de jogo", simula o trabalho de CPU de um frame com 30 mil unidades em dois times
 *    (grade espacial em série, física/colisão/IA em paralelo, lógica e preparação do desenho em série).
 *  - st_cpu / st_ram: carga contínua (AVX2+FMA quando há) e escrita/verificação de padrões na RAM.
 *
 * Ideias e cena adaptadas do ResetExplode (BenchRenderer.cs, GameSimBench.cs, StressTester.cs;
 * licença MIT, © 2026 Gustavo), reescritas em C.
 */

#include <math.h>

#ifdef _WIN32
typedef HANDLE Fio;
#define FIO_FUNC(nome) static DWORD WINAPI nome(LPVOID arg)
#define FIO_FIM return 0
static Fio cria_fio(LPTHREAD_START_ROUTINE f, void *arg) { return CreateThread(NULL, 0, f, arg, 0, NULL); }
static void espera_fio(Fio f) { WaitForSingleObject(f, INFINITE); CloseHandle(f); }
static void prioridade_baixa(void) { SetThreadPriority(GetCurrentThread(), THREAD_PRIORITY_BELOW_NORMAL); }
static void cede(void) { SwitchToThread(); }
#else
typedef pthread_t Fio;
#define FIO_FUNC(nome) static void *nome(void *arg)
#define FIO_FIM return NULL
static Fio cria_fio(void *(*f)(void *), void *arg) { pthread_t t; pthread_create(&t, NULL, f, arg); return t; }
static void espera_fio(Fio f) { pthread_join(f, NULL); }
static void prioridade_baixa(void) { }
static void cede(void) { sched_yield(); }
#endif

static uint32_t rnd32(uint32_t *s) {
    uint32_t x = *s;
    x ^= x << 13; x ^= x >> 17; x ^= x << 5;
    *s = x;
    return x;
}
static float rndf(uint32_t *s) { return (rnd32(s) >> 8) * (1.0f / 16777216.0f); }

/* ================================================================== path tracer */

#define BC_L 960
#define BC_A 540
#define BC_BLOCO 32
#define BC_SPP 512
#define BC_PROF 6
#define BC_MAX_BLOCOS 1024

typedef struct { float x, y, z; } V3;
static V3 v3(float x, float y, float z) { V3 r = {x, y, z}; return r; }
static V3 soma(V3 a, V3 b) { return v3(a.x + b.x, a.y + b.y, a.z + b.z); }
static V3 sub(V3 a, V3 b) { return v3(a.x - b.x, a.y - b.y, a.z - b.z); }
static V3 esc(V3 a, float k) { return v3(a.x * k, a.y * k, a.z * k); }
static float dot(V3 a, V3 b) { return a.x * b.x + a.y * b.y + a.z * b.z; }
static V3 cruz(V3 a, V3 b) { return v3(a.y * b.z - a.z * b.y, a.z * b.x - a.x * b.z, a.x * b.y - a.y * b.x); }
static V3 normal(V3 a) { return esc(a, 1.0f / sqrtf(dot(a, a))); }
static V3 reflete(V3 d, V3 n) { return sub(d, esc(n, 2 * dot(d, n))); }

enum { DIFUSO, METAL, VIDRO, LUZ };
typedef struct { V3 c; float r; int mat; float albedo, aspereza; } Esfera;

static Esfera cena[32];
static int n_cena;
static const Esfera lampada = {{-4.0f, 7.0f, -3.0f}, 2.2f, LUZ, 0, 0};
static const float potencia_lampada = 9.0f;
static V3 cam_origem, cam_canto, cam_horizontal, cam_vertical;
static int bc_blocos[BC_MAX_BLOCOS][4];
static int bc_n_blocos;
static volatile int bc_pronto;

static int cmp_dist(const void *a, const void *b) {
    const int *x = a, *y = b;
    float cx = BC_L / 2.0f, cy = BC_A / 2.0f;
    float da = powf(x[0] + x[2] / 2.0f - cx, 2) + powf((x[1] + x[3] / 2.0f - cy) * 1.6f, 2);
    float db = powf(y[0] + y[2] / 2.0f - cx, 2) + powf((y[1] + y[3] / 2.0f - cy) * 1.6f, 2);
    return da < db ? -1 : da > db ? 1 : 0;
}

static void bc_preparar(void) {
    if (bc_pronto) return;
    Esfera base[] = {
        {{0, 1.1f, 0}, 1.1f, VIDRO, 0.98f, 0},
        {{-2.5f, 0.9f, 0.9f}, 0.9f, METAL, 0.92f, 0.03f},
        {{2.4f, 0.8f, 0.7f}, 0.8f, DIFUSO, 0.85f, 0},
        {{1.0f, 0.45f, -1.9f}, 0.45f, METAL, 0.85f, 0.25f},
        {{-1.1f, 0.35f, -2.0f}, 0.35f, DIFUSO, 0.15f, 0},
    };
    n_cena = 0;
    for (int i = 0; i < 5; i++) cena[n_cena++] = base[i];
    for (int i = 0; i < 14; i++) {  /* anel de esferas pequenas alternando materiais */
        float a = i / 14.0f * 6.2831853f;
        Esfera e = {{cosf(a) * 4.2f, 0.28f, sinf(a) * 4.2f + 1.5f}, 0.28f, i % 3 == 0 ? METAL : i % 3 == 1 ? DIFUSO : VIDRO,
                    i % 2 == 0 ? 0.9f : 0.3f, 0.1f};
        cena[n_cena++] = e;
    }
    V3 alvo = v3(0, 0.75f, 0);
    cam_origem = v3(0, 2.1f, -7.2f);
    float vfov = 38.0f * 3.14159265f / 180, aspecto = (float)BC_L / BC_A;
    float hh = tanf(vfov / 2), hw = aspecto * hh;
    V3 w = normal(sub(cam_origem, alvo));
    V3 u = normal(cruz(v3(0, 1, 0), w));
    V3 v = cruz(w, u);
    cam_canto = sub(soma(sub(cam_origem, esc(u, hw)), esc(v, hh)), w);
    cam_horizontal = esc(u, 2 * hw);
    cam_vertical = esc(v, -2 * hh);
    bc_n_blocos = 0;
    for (int y = 0; y < BC_A; y += BC_BLOCO)
        for (int x = 0; x < BC_L; x += BC_BLOCO) {
            int *b = bc_blocos[bc_n_blocos++];
            b[0] = x; b[1] = y;
            b[2] = BC_L - x < BC_BLOCO ? BC_L - x : BC_BLOCO;
            b[3] = BC_A - y < BC_BLOCO ? BC_A - y : BC_BLOCO;
        }
    qsort(bc_blocos, bc_n_blocos, sizeof(bc_blocos[0]), cmp_dist);  /* do centro para fora */
    bc_pronto = 1;
}

static int bate_esfera(const Esfera *s, V3 o, V3 d, float tmax, float *t) {
    V3 oc = sub(o, s->c);
    float b = dot(oc, d), c = dot(oc, oc) - s->r * s->r, disc = b * b - c;
    if (disc < 0) return 0;
    float r = sqrtf(disc), x = -b - r;
    if (x < 1e-4f) x = -b + r;
    if (x > 1e-4f && x < tmax) { *t = x; return 1; }
    return 0;
}

/* idx: -1 chão, >= 0 esfera da cena; *lamp = 1 quando bate na lâmpada. */
static int intersecta(V3 o, V3 d, float *tmin, int *idx, int *lamp) {
    float t;
    *tmin = 3.4e38f; *idx = -2; *lamp = 0;
    if (d.y < -1e-6f) {
        t = -o.y / d.y;
        if (t > 1e-4f && t < 60) { *tmin = t; *idx = -1; }
    }
    for (int i = 0; i < n_cena; i++)
        if (bate_esfera(&cena[i], o, d, *tmin, &t)) { *tmin = t; *idx = i; }
    if (bate_esfera(&lampada, o, d, *tmin, &t)) { *tmin = t; *lamp = 1; }
    return *idx != -2 || *lamp;
}

static void base_ortonormal(V3 n, V3 *a, V3 *b) {
    *a = normal(cruz(fabsf(n.x) > 0.9f ? v3(0, 1, 0) : v3(1, 0, 0), n));
    *b = cruz(n, *a);
}

static float luz_direta(V3 p, V3 n, uint32_t *s) {
    V3 para = sub(lampada.c, p);
    float d2 = dot(para, para);
    float cosmax = sqrtf(fmaxf(0, 1 - lampada.r * lampada.r / d2));
    V3 w = esc(para, 1.0f / sqrtf(d2)), a, b;
    float r1 = rndf(s), r2 = rndf(s);
    float cost = 1 - r1 * (1 - cosmax), sint = sqrtf(1 - cost * cost), phi = 6.2831853f * r2;
    base_ortonormal(w, &a, &b);
    V3 dir = normal(soma(soma(esc(a, cosf(phi) * sint), esc(b, sinf(phi) * sint)), esc(w, cost)));
    float cosn = dot(dir, n), t;
    int idx, lamp;
    if (cosn <= 0) return 0;
    if (!intersecta(soma(p, esc(n, 1e-3f)), dir, &t, &idx, &lamp) || !lamp) return 0;
    return potencia_lampada * cosn * 2 * (1 - cosmax);
}

static float tracar(V3 o, V3 d, uint32_t *s) {
    float radiancia = 0, fluxo = 1;
    int especular = 1;  /* a câmera e os reflexos "veem" a lâmpada diretamente */
    for (int prof = 0; prof < BC_PROF; prof++) {
        float t;
        int idx, lamp;
        if (!intersecta(o, d, &t, &idx, &lamp)) { radiancia += fluxo * (0.06f + 0.22f * fmaxf(0, d.y)); break; }
        if (lamp) { if (especular) radiancia += fluxo * potencia_lampada; break; }
        V3 p = soma(o, esc(d, t)), n;
        int mat;
        float albedo, aspereza = 0;
        if (idx < 0) {  /* chão em xadrez cinza */
            n = v3(0, 1, 0); mat = DIFUSO;
            albedo = ((((int)floorf(p.x * 0.9f)) + ((int)floorf(p.z * 0.9f))) & 1) == 0 ? 0.62f : 0.38f;
        } else {
            const Esfera *e = &cena[idx];
            n = esc(sub(p, e->c), 1.0f / e->r); mat = e->mat; albedo = e->albedo; aspereza = e->aspereza;
        }
        if (mat == DIFUSO) {
            radiancia += fluxo * albedo * luz_direta(p, n, s);
            fluxo *= albedo;
            float r1 = rndf(s), r2 = rndf(s), r = sqrtf(r1), phi = 6.2831853f * r2;
            V3 a, b;
            base_ortonormal(n, &a, &b);
            d = normal(soma(soma(esc(a, r * cosf(phi)), esc(b, r * sinf(phi))), esc(n, sqrtf(1 - r1))));
            o = soma(p, esc(n, 1e-3f));
            especular = 0;
        } else if (mat == METAL) {
            V3 q;
            do { q = v3(rndf(s) * 2 - 1, rndf(s) * 2 - 1, rndf(s) * 2 - 1); } while (dot(q, q) >= 1);
            V3 r = soma(reflete(d, n), esc(q, aspereza));
            if (dot(r, n) <= 0) return radiancia;
            d = normal(r);
            o = soma(p, esc(n, 1e-3f));
            fluxo *= albedo;
            especular = 1;
        } else {  /* vidro */
            const float ior = 1.5f;
            int fora = dot(d, n) < 0;
            V3 nn = fora ? n : esc(n, -1);
            float eta = fora ? 1 / ior : ior;
            float c = fminf(-dot(d, nn), 1), sin2 = eta * eta * (1 - c * c);
            float r0 = (1 - ior) / (1 + ior); r0 *= r0;
            float fresnel = r0 + (1 - r0) * powf(1 - c, 5);
            if (sin2 > 1 || rndf(s) < fresnel) {
                d = reflete(d, nn);
                o = soma(p, esc(nn, 1e-3f));
            } else {
                d = normal(soma(esc(d, eta), esc(nn, eta * c - sqrtf(1 - sin2))));
                o = sub(p, esc(nn, 1e-3f));
            }
            fluxo *= albedo;
            especular = 1;
        }
        if (prof >= 3) {  /* roleta russa */
            float q = fmaxf(0.05f, 1 - fluxo);
            if (rndf(s) < q) break;
            fluxo /= 1 - q;
        }
    }
    return radiancia;
}

static uint8_t para_cinza(float v) {
    v *= 1.1f;
    v = v * (2.51f * v + 0.03f) / (v * (2.43f * v + 0.59f) + 0.14f);  /* ACES aproximado */
    v = v < 0 ? 0 : v > 1 ? 1 : v;
    return (uint8_t)(powf(v, 1 / 2.2f) * 255 + 0.5f);
}

/* Estado compartilhado com o Python (lido enquanto o teste roda). */
typedef struct {
    volatile int32_t cancelar;
    volatile int32_t blocos_feitos;
    volatile int32_t blocos_total;
    volatile int32_t passadas;
    volatile int64_t amostras;
    volatile int32_t ativos[MAX_FIOS];  /* bloco que cada thread está desenhando (-1 = livre) */
} EstadoRender;

typedef struct {
    EstadoRender *e;
    uint8_t *pixels;
    volatile int32_t *proximo;
    int passo, indice;
} TrabalhoRender;

static void desenha_bloco(const int *b, uint8_t *pixels, EstadoRender *e) {
    V3 o = cam_origem;
    for (int y = b[1]; y < b[1] + b[3]; y++) {
        if (e->cancelar) return;
        for (int x = b[0]; x < b[0] + b[2]; x++) {
            uint32_t semente_px = (uint32_t)(y * BC_L + x) * 9781u + 0x9E3779B9u;
            float total = 0;
            for (int k = 0; k < BC_SPP; k++) {
                float u = (x + rndf(&semente_px)) / BC_L, v = (y + rndf(&semente_px)) / BC_A;
                V3 d = normal(sub(soma(soma(cam_canto, esc(cam_horizontal, u)), esc(cam_vertical, v)), o));
                total += fminf(tracar(o, d, &semente_px), 4.0f);  /* limita "vagalumes" */
            }
            pixels[y * BC_L + x] = para_cinza(total / BC_SPP);
        }
        __sync_fetch_and_add(&e->amostras, (int64_t)b[2] * BC_SPP);
    }
}

FIO_FUNC(fio_render) {
    TrabalhoRender *t = arg;
    int i;
    while (!t->e->cancelar && (i = __sync_fetch_and_add(t->proximo, 1)) * t->passo < bc_n_blocos) {
        int b = i * t->passo;
        t->e->ativos[t->indice] = b;
        desenha_bloco(bc_blocos[b], t->pixels, t->e);
        __sync_fetch_and_add(&t->e->blocos_feitos, 1);
    }
    t->e->ativos[t->indice] = -1;
    FIO_FIM;
}

EXPORTA void bc_dimensoes(int32_t out[4]) {
    bc_preparar();
    out[0] = BC_L; out[1] = BC_A; out[2] = bc_n_blocos; out[3] = BC_SPP;
}

EXPORTA void bc_bloco(int indice, int32_t out[4]) {
    bc_preparar();
    if (indice < 0 || indice >= bc_n_blocos) { out[0] = out[1] = out[2] = out[3] = 0; return; }
    for (int k = 0; k < 4; k++) out[k] = bc_blocos[indice][k];
}

/*
 * Renderiza a imagem com `threads` threads. `passo` = 1 desenha todos os blocos; `passo` = N desenha um a
 * cada N (o teste de 1 núcleo usa uma amostra espalhada da imagem, com a mesma mistura de materiais).
 * Repete passadas até completar `min_segundos`. Devolve a nota (amostras por segundo / 10.000) ou -1 se
 * cancelado.
 */
EXPORTA double bc_render(int threads, int passo, double min_segundos, EstadoRender *e, uint8_t *pixels) {
    bc_preparar();
    if (threads < 1) threads = 1;
    if (threads > MAX_FIOS) threads = MAX_FIOS;
    if (passo < 1) passo = 1;
    e->amostras = 0;
    e->passadas = 0;
    e->blocos_total = (bc_n_blocos + passo - 1) / passo;
    double t0 = agora_s();
    do {
        volatile int32_t proximo = 0;
        e->blocos_feitos = 0;
        TrabalhoRender trab[MAX_FIOS];
        Fio fios[MAX_FIOS];
        for (int i = 0; i < threads; i++) {
            trab[i] = (TrabalhoRender){e, pixels, &proximo, passo, i};
            e->ativos[i] = -1;
            fios[i] = cria_fio(fio_render, &trab[i]);
        }
        for (int i = 0; i < threads; i++) espera_fio(fios[i]);
        if (e->cancelar) return -1;
        e->passadas++;
    } while (agora_s() - t0 < min_segundos);
    double seg = agora_s() - t0;
    return seg > 0 ? e->amostras / seg / 10000.0 : 0;
}

/* ================================================================== simulação de jogo */

#define GS_N 30000
#define GS_MUNDO 1000.0f
#define GS_GRADE 64
#define GS_CELULA (GS_MUNDO / GS_GRADE)
#define GS_RAIO GS_CELULA
#define GS_DT (1.0f / 60.0f)
#define GS_MAX_QUADROS 200000

static float gs_xa[GS_N], gs_ya[GS_N], gs_vxa[GS_N], gs_vya[GS_N];
static float gs_xb[GS_N], gs_yb[GS_N], gs_vxb[GS_N], gs_vyb[GS_N];
static float *gs_x = gs_xa, *gs_y = gs_ya, *gs_vx = gs_vxa, *gs_vy = gs_vya;
static float *gs_nx = gs_xb, *gs_ny = gs_yb, *gs_nvx = gs_vxb, *gs_nvy = gs_vyb;
static float gs_vida[GS_N], gs_dano[GS_N];
static uint8_t gs_time[GS_N];
static int gs_inicio_cel[GS_GRADE * GS_GRADE + 1], gs_preenche[GS_GRADE * GS_GRADE + 1];
static int gs_ordenado[GS_N], gs_cel_de[GS_N];
typedef struct { float chave; int i; } Desenho;
static Desenho gs_desenho[GS_N];
static float gs_tempos[GS_MAX_QUADROS];
static uint32_t gs_rng;
static int gs_quadro;

typedef struct {
    volatile int32_t cancelar;
    volatile int32_t aquecendo;
    volatile int32_t quadros;
    float progresso;
    float fps_atual;
} EstadoSim;

typedef struct {
    double fps_medio, low1, ms_principal, ms_paralelo, segundos;
    int32_t quadros, threads;
} ResultadoSim;

static void gs_reiniciar(void) {
    gs_rng = 12345;
    gs_x = gs_xa; gs_y = gs_ya; gs_vx = gs_vxa; gs_vy = gs_vya;
    gs_nx = gs_xb; gs_ny = gs_yb; gs_nvx = gs_vxb; gs_nvy = gs_vyb;
    gs_quadro = 0;
    for (int i = 0; i < GS_N; i++) {
        gs_time[i] = (uint8_t)(i & 1);
        gs_x[i] = (gs_time[i] == 0 ? 0.05f : 0.55f) * GS_MUNDO + rndf(&gs_rng) * GS_MUNDO * 0.4f;
        gs_y[i] = rndf(&gs_rng) * GS_MUNDO;
        gs_vx[i] = (gs_time[i] == 0 ? 1 : -1) * (20 + rndf(&gs_rng) * 10);
        gs_vy[i] = (rndf(&gs_rng) - 0.5f) * 10;
        gs_vida[i] = 100;
    }
}

static int celula(float v) {
    int c = (int)(v / GS_CELULA);
    return c < 0 ? 0 : c >= GS_GRADE ? GS_GRADE - 1 : c;
}

static void gs_grade(void) {  /* counting sort por célula */
    memset(gs_inicio_cel, 0, sizeof(gs_inicio_cel));
    for (int i = 0; i < GS_N; i++) {
        int c = celula(gs_y[i]) * GS_GRADE + celula(gs_x[i]);
        gs_cel_de[i] = c;
        gs_inicio_cel[c + 1]++;
    }
    for (int c = 0; c < GS_GRADE * GS_GRADE; c++) gs_inicio_cel[c + 1] += gs_inicio_cel[c];
    memcpy(gs_preenche, gs_inicio_cel, sizeof(gs_inicio_cel));
    for (int i = 0; i < GS_N; i++) gs_ordenado[gs_preenche[gs_cel_de[i]]++] = i;
}

static void gs_simular(int i) {  /* física + colisão + IA de uma unidade; lê o estado atual, escreve só a dela */
    float x = gs_x[i], y = gs_y[i], vx = gs_vx[i], vy = gs_vy[i];
    uint8_t time = gs_time[i];
    float sepx = 0, sepy = 0, alix = 0, aliy = 0, cohx = 0, cohy = 0, dano = 0;
    float melhor = GS_RAIO * GS_RAIO, ix = 0, iy = 0;
    int aliados = 0, inimigo = 0;
    int c = gs_cel_de[i], cx = c % GS_GRADE, cy = c / GS_GRADE;
    for (int gy = cy > 0 ? cy - 1 : 0; gy <= (cy < GS_GRADE - 1 ? cy + 1 : cy); gy++)
        for (int gx = cx > 0 ? cx - 1 : 0; gx <= (cx < GS_GRADE - 1 ? cx + 1 : cx); gx++) {
            int cel = gy * GS_GRADE + gx;
            for (int k = gs_inicio_cel[cel]; k < gs_inicio_cel[cel + 1]; k++) {
                int j = gs_ordenado[k];
                if (j == i) continue;
                float dx = gs_x[j] - x, dy = gs_y[j] - y, d2 = dx * dx + dy * dy;
                if (d2 > GS_RAIO * GS_RAIO || d2 < 1e-6f) continue;
                float inv = 1 / sqrtf(d2);
                if (d2 < 9) { sepx -= dx * inv; sepy -= dy * inv; }
                if (gs_time[j] == time) {
                    alix += gs_vx[j]; aliy += gs_vy[j]; cohx += dx; cohy += dy; aliados++;
                } else {
                    if (d2 < 16) dano += 4 * GS_DT * (1 - sqrtf(d2) / 4);
                    if (d2 < melhor) { melhor = d2; ix = dx; iy = dy; inimigo = 1; }
                }
            }
        }
    float ax = sepx * 60, ay = sepy * 60;
    if (aliados > 0) {
        ax += (alix / aliados - vx) * 0.5f + cohx / aliados * 0.3f;
        ay += (aliy / aliados - vy) * 0.5f + cohy / aliados * 0.3f;
    }
    if (inimigo) {  /* vida baixa foge do inimigo mais próximo; senão persegue */
        float inv = 1 / sqrtf(melhor), dir = gs_vida[i] < 30 ? -1.0f : 1.0f;
        ax += dir * ix * inv * 80; ay += dir * iy * inv * 80;
    } else ax += (time == 0 ? 1 : -1) * 25;
    vx += ax * GS_DT; vy += ay * GS_DT;
    float v = sqrtf(vx * vx + vy * vy);
    if (v > 60) { vx *= 60 / v; vy *= 60 / v; }
    x += vx * GS_DT; y += vy * GS_DT;
    if (x < 0 || x > GS_MUNDO) { vx = -vx; x = x < 0 ? 0 : GS_MUNDO; }
    if (y < 0 || y > GS_MUNDO) { vy = -vy; y = y < 0 ? 0 : GS_MUNDO; }
    gs_nx[i] = x; gs_ny[i] = y; gs_nvx[i] = vx; gs_nvy[i] = vy;
    gs_dano[i] = dano;
}

static void gs_logica(void) {  /* dano, mortes e renascimento na própria base */
    for (int i = 0; i < GS_N; i++) {
        gs_vida[i] = fminf(100, gs_vida[i] - gs_dano[i] + 0.5f * GS_DT);
        int pontuou = gs_time[i] == 0 ? gs_x[i] > GS_MUNDO * 0.97f : gs_x[i] < GS_MUNDO * 0.03f;
        if (gs_vida[i] > 0 && !pontuou) continue;
        gs_vida[i] = 100;
        gs_x[i] = (gs_time[i] == 0 ? 0.02f : 0.98f) * GS_MUNDO;
        gs_y[i] = rndf(&gs_rng) * GS_MUNDO;
        gs_vx[i] = (gs_time[i] == 0 ? 1 : -1) * 25.0f; gs_vy[i] = 0;
    }
}

static int cmp_desenho(const void *a, const void *b) {
    float x = ((const Desenho *)a)->chave, y = ((const Desenho *)b)->chave;
    if (x != y) return x < y ? -1 : 1;
    return ((const Desenho *)a)->i - ((const Desenho *)b)->i;
}

static void gs_preparar_desenho(void) {  /* câmera em perspectiva, culling e ordem de trás para a frente */
    float camx = GS_MUNDO / 2 + sinf(gs_quadro * 0.01f) * 200, camy = -200;
    int visiveis = 0;
    for (int i = 0; i < GS_N; i++) {
        float dx = gs_x[i] - camx, dy = gs_y[i] - camy;
        if (dy <= 0 || fabsf(dx) > dy * 1.2f) continue;
        gs_desenho[visiveis].chave = -dy;
        gs_desenho[visiveis++].i = i;
    }
    qsort(gs_desenho, visiveis, sizeof(Desenho), cmp_desenho);
}

/* Fila de trabalho do trecho paralelo: blocos pegos por contador atômico; a thread principal também trabalha. */
static volatile int32_t gs_geracao, gs_proximo, gs_feitos, gs_sair;
static int gs_blocos, gs_por_bloco;

static void gs_trabalhar(void) {
    int b;
    while ((b = __sync_fetch_and_add(&gs_proximo, 1)) < gs_blocos) {
        int fim = (b + 1) * gs_por_bloco < GS_N ? (b + 1) * gs_por_bloco : GS_N;
        for (int i = b * gs_por_bloco; i < fim; i++) gs_simular(i);
        __sync_fetch_and_add(&gs_feitos, 1);
    }
}

FIO_FUNC(fio_sim) {
    (void)arg;
    int32_t visto = 0;
    for (;;) {
        int voltas = 0;
        while (gs_geracao == visto && !gs_sair) {
            _mm_pause();
            if (++voltas > 4000) cede();
        }
        if (gs_sair) break;
        visto = gs_geracao;
        gs_trabalhar();
    }
    FIO_FIM;
}

static void gs_passo(double *ms_principal, double *ms_paralelo) {
    double t0 = agora_s();
    gs_grade();
    double t1 = agora_s();
    gs_feitos = 0;
    __sync_synchronize();
    gs_proximo = 0;
    __sync_fetch_and_add(&gs_geracao, 1);
    gs_trabalhar();
    while (gs_feitos < gs_blocos) _mm_pause();
    double t2 = agora_s();
    float *t;
    t = gs_x; gs_x = gs_nx; gs_nx = t;
    t = gs_y; gs_y = gs_ny; gs_ny = t;
    t = gs_vx; gs_vx = gs_nvx; gs_nvx = t;
    t = gs_vy; gs_vy = gs_nvy; gs_nvy = t;
    gs_logica();
    gs_preparar_desenho();
    gs_quadro++;
    double t3 = agora_s();
    *ms_principal = (t1 - t0 + t3 - t2) * 1000;
    *ms_paralelo = (t2 - t1) * 1000;
}

static int cmp_float(const void *a, const void *b) {
    float x = *(const float *)a, y = *(const float *)b;
    return x < y ? -1 : x > y ? 1 : 0;
}

/* Roda a simulação por `segundos` (depois de 2 s de aquecimento). Devolve 0, ou -1 se cancelada. */
EXPORTA int gs_rodar(int threads, double segundos, EstadoSim *e, ResultadoSim *r) {
    if (threads < 1) threads = 1;
    if (threads > MAX_FIOS) threads = MAX_FIOS;
    memset(r, 0, sizeof(*r));
    gs_reiniciar();
    gs_blocos = threads * 4;
    gs_por_bloco = (GS_N + gs_blocos - 1) / gs_blocos;
    gs_sair = 0;
    gs_geracao = 0;
    gs_proximo = gs_blocos;
    Fio fios[MAX_FIOS];
    for (int i = 0; i < threads - 1; i++) fios[i] = cria_fio(fio_sim, NULL);

    double mp, mpar, soma_p = 0, soma_par = 0;
    e->aquecendo = 1;
    double t0 = agora_s();
    while (agora_s() - t0 < 2 && !e->cancelar) gs_passo(&mp, &mpar);
    e->aquecendo = 0;
    int n = 0, janela = 0;
    double inicio = agora_s(), marco = inicio;
    while (!e->cancelar) {
        double a = agora_s();
        if (a - inicio >= segundos) break;
        gs_passo(&mp, &mpar);
        double b = agora_s();
        if (n < GS_MAX_QUADROS) gs_tempos[n] = (float)((b - a) * 1000);
        n++;
        janela++;
        soma_p += mp; soma_par += mpar;
        e->quadros = n;
        e->progresso = (float)((b - inicio) / segundos);
        if (b - marco >= 0.5) { e->fps_atual = (float)(janela / (b - marco)); janela = 0; marco = b; }
    }
    double fim = agora_s();
    gs_sair = 1;
    for (int i = 0; i < threads - 1; i++) espera_fio(fios[i]);
    if (e->cancelar) return -1;
    int guardados = n < GS_MAX_QUADROS ? n : GS_MAX_QUADROS;
    qsort(gs_tempos, guardados, sizeof(float), cmp_float);
    double p99 = guardados ? gs_tempos[(int)(guardados * 0.99) < guardados ? (int)(guardados * 0.99) : guardados - 1] : 0;
    r->segundos = fim - inicio;
    r->quadros = n;
    r->threads = threads;
    r->fps_medio = r->segundos > 0 ? n / r->segundos : 0;
    r->low1 = p99 > 0 ? 1000 / p99 : 0;
    r->ms_principal = n ? soma_p / n : 0;
    r->ms_paralelo = n ? soma_par / n : 0;
    return 0;
}

/* Vista de cima da batalha em RGB (lado x lado): time A em azul, time B em laranja. */
EXPORTA void gs_desenhar(uint8_t *rgb, int lado) {
    for (int i = 0; i < lado * lado; i++) { rgb[3 * i] = 14; rgb[3 * i + 1] = 16; rgb[3 * i + 2] = 20; }
    float k = lado / GS_MUNDO;
    const float *xs = gs_x, *ys = gs_y;
    for (int i = 0; i < GS_N; i++) {
        int px = (int)(xs[i] * k), py = (int)(ys[i] * k);
        if (px < 0 || py < 0 || px >= lado || py >= lado) continue;
        uint8_t *p = rgb + 3 * (py * lado + px);
        if (gs_time[i] == 0) { p[0] = 76; p[1] = 194; p[2] = 255; }
        else { p[0] = 255; p[1] = 150; p[2] = 70; }
    }
}

/* ================================================================== estresse */

typedef struct {
    volatile int32_t cancelar;
    volatile int64_t operacoes;     /* CPU: blocos de cálculo feitos (para a nota de estabilidade) */
    volatile int64_t passadas;      /* RAM: passadas completas (soma de todas as threads) */
    volatile int64_t erros;         /* RAM: palavras que voltaram diferentes do que foi gravado */
    volatile int64_t alocado;       /* RAM: bytes alocados de fato */
} EstadoEstresse;

/* 12 cadeias independentes de FMA de 256 bits deixam as duas unidades de ponto flutuante ocupadas o tempo todo
 * (o que mais esquenta e consome, como no Prime95); a raiz quadrada de vez em quando varia a carga. */
__attribute__((target("avx2,fma")))
static void carga_avx2(EstadoEstresse *e, int semente) {
    __m256d v[12], mul = _mm256_set1_pd(0.99999999), add = _mm256_set1_pd(1e-8);
    for (int k = 0; k < 12; k++) v[k] = _mm256_set_pd(semente + k + 1.0, semente + k + 2.0, semente + k + 3.0, semente + k + 4.0);
    while (!e->cancelar) {
        for (int rep = 0; rep < 8192; rep++)
            for (int k = 0; k < 12; k++) v[k] = _mm256_fmadd_pd(v[k], mul, add);
        v[0] = _mm256_sqrt_pd(v[0]);
        __sync_fetch_and_add(&e->operacoes, 1);
    }
    double soma_v[4];
    _mm256_storeu_pd(soma_v, v[0]);
    if (soma_v[0] == 0) e->operacoes++;  /* impede o compilador de descartar o cálculo */
}

static void carga_escalar(EstadoEstresse *e, int semente) {
    double buf[512];
    uint64_t x = (uint64_t)(semente + 1) * 0x9E3779B97F4A7C15ULL;
    for (int i = 0; i < 512; i++) buf[i] = i + semente + 1;
    while (!e->cancelar) {
        for (int rep = 0; rep < 64; rep++)
            for (int i = 0; i < 512; i++) {
                double v = buf[i] * 1.0000001 + 0.5;
                v = sqrt(v * v + 1.0) + sin(v) * 1e-3;
                buf[i] = v > 1e12 ? i + 1 : v;
                x ^= x << 13; x ^= x >> 7; x ^= x << 17;
            }
        __sync_fetch_and_add(&e->operacoes, 1);
    }
    if (x == 0) e->operacoes++;
}

typedef struct { EstadoEstresse *e; int semente, avx2; } TrabalhoCpu;

FIO_FUNC(fio_cpu) {
    TrabalhoCpu *t = arg;
    prioridade_baixa();  /* o painel continua respondendo; os núcleos seguem a 100% */
    if (t->avx2) carga_avx2(t->e, t->semente);
    else carga_escalar(t->e, t->semente);
    FIO_FIM;
}

/* Bloqueia até `cancelar`. */
EXPORTA void st_cpu(int threads, EstadoEstresse *e) {
    if (threads < 1) threads = 1;
    if (threads > MAX_FIOS) threads = MAX_FIOS;
    int avx2 = mt_isa() >= 2;
    TrabalhoCpu trab[MAX_FIOS];
    Fio fios[MAX_FIOS];
    for (int i = 0; i < threads; i++) {
        trab[i] = (TrabalhoCpu){e, i, avx2};
        fios[i] = cria_fio(fio_cpu, &trab[i]);
    }
    for (int i = 0; i < threads; i++) espera_fio(fios[i]);
}

#define ST_BLOCO ((size_t)128 << 20)
#define ST_MAX_BLOCOS 1024

typedef struct { EstadoEstresse *e; uint64_t **blocos; int n; } TrabalhoRam;

FIO_FUNC(fio_ram) {
    TrabalhoRam *t = arg;
    size_t palavras = ST_BLOCO / 8;
    uint64_t passada = 0;
    prioridade_baixa();
    while (!t->e->cancelar) {
        passada++;
        uint64_t padrao = passada * 0x9E3779B97F4A7C15ULL;
        for (int b = 0; b < t->n && !t->e->cancelar; b++) {
            uint64_t *p = t->blocos[b];
            for (size_t i = 0; i < palavras; i++) p[i] = padrao ^ i;
        }
        for (int b = 0; b < t->n && !t->e->cancelar; b++) {
            volatile uint64_t *p = t->blocos[b];
            int64_t erros = 0;
            for (size_t i = 0; i < palavras; i++) erros += p[i] != (padrao ^ i);
            if (erros) __sync_fetch_and_add(&t->e->erros, erros);
        }
        if (!t->e->cancelar) __sync_fetch_and_add(&t->e->passadas, 1);
    }
    FIO_FIM;
}

/* Aloca até `bytes` em blocos de 128 MB e grava/verifica até `cancelar`. Devolve os bytes usados. */
EXPORTA int64_t st_ram(int64_t bytes, int threads, EstadoEstresse *e) {
    static uint64_t *blocos[ST_MAX_BLOCOS];
    int n = 0;
    if (threads < 1) threads = 1;
    if (threads > 16) threads = 16;
    while ((int64_t)((n + 1) * ST_BLOCO) <= bytes && n < ST_MAX_BLOCOS && !e->cancelar) {
        void *p = aloca(ST_BLOCO);
        if (!p) break;
        blocos[n++] = p;
    }
    e->alocado = (int64_t)n * ST_BLOCO;
    if (n == 0) return 0;
    if (threads > n) threads = n;
    TrabalhoRam trab[16];
    Fio fios[16];
    int por = n / threads, resto = n % threads, ini = 0;
    for (int i = 0; i < threads; i++) {
        int qtd = por + (i < resto);
        trab[i] = (TrabalhoRam){e, blocos + ini, qtd};
        ini += qtd;
        fios[i] = cria_fio(fio_ram, &trab[i]);
    }
    for (int i = 0; i < threads; i++) espera_fio(fios[i]);
    for (int i = 0; i < n; i++) libera(blocos[i], ST_BLOCO);
    return e->alocado;
}
