/* gw_game.h: the rules of GROUNDWAVE, a turn-based artillery game for the K6 (c/k6/mod/gw.c).

   Two radio stations sit on hills, a mast on each shack, and take turns lobbing radio-themed
   projectiles at each other across a 128 x 30 pixel field (the level-6 bitmap). Pure C, no API
   calls and no floating point: every step is integer arithmetic on fixed-point numbers (Q16
   pixels), and the only randomness is the game's own xorshift generator. So two radios that
   start from the same seed and apply the same moves stay in step to the bit, which is what play
   over the air relies on: a move is three bytes (angle, power, item), and each radio works out
   the flight itself. c/k6/gw_host.c builds this file for the host (tests, and the PC opponent
   in modem/k6gw.py).

   The field: top[x] is the first dirt row of column x (row 0 is the sky's top, GW_H - 1 the
   bedrock, never dug). A blast takes a disc out of the dirt and the dirt above the hole falls
   into it, so the terrain stays one height per column. A station stands on the highest dirt
   under its 5-pixel footprint; when that drops, it falls and takes damage.

   The M0 has no divide instruction and a module has no run-time library: divisions go through
   gw_udiv, products stay inside 32 bits, and there are no writable statics (the state lives in
   the caller's gw_t). SPDX-License-Identifier: MIT */
#ifndef GW_GAME_H
#define GW_GAME_H
#include <stdint.h>

#define GW_W 128                /* field width, pixels */
#define GW_H 30                 /* field height (KM_WF_ROWS) */
#define GW_BED (GW_H - 1)       /* bedrock row: never dug */
#define GW_HP 100
#define GW_NI 14                /* shop items: weapons 0-11, shields 12-13 */
#define GW_NWPN 12
#define GW_NS 6                 /* shots in the air at once */
#define GW_NB 8                 /* blasts noted in one step, for the screen */
#define GW_CEIL 8               /* the Beverage's ceiling row */
#define GW_G 655                /* gravity per unit of an item's gr (4 = normal), Q16 px/step^2 */
#define GW_VK 1606              /* launch speed per power point, Q16 px/step (100: 2.45 px/step) */
#define GW_WK 39                /* wind's push per unit, Q16 px/step^2 */
#define GW_WMAX 9
#define GW_START 80             /* dollars at the start of a game */
#define GW_ROUND_PAY 30         /* to each station after a round */
#define GW_WIN_PAY 40           /* and to the round's winner */
#define GW_MAX_AGE 1500         /* a shot still flying after this many steps is dropped */
#define GW_BEAM_STEPS 70
#define GW_SHIELD_K 6000        /* a shield's push: gr * K / distance^2, Q16 px/step^2 per px */
#define GW_YS 17                /* y is Q17 rows: the sky is drawn at half height, so arcs that
                                   would peak far above a 30-row field mostly stay on it */

enum {
  GF_NOWIND = 1,                /* the wind doesn't move it */
  GF_WANDER = 2,                /* a random kick every step (The Lid) */
  GF_CEIL = 4,                  /* can't climb above GW_CEIL (Beverage) */
  GF_BOUNCE = 8,                /* skips off the ground once before it goes off */
  GF_SPLIT = 16,                /* splits in three at the top of its arc (Repeater) */
  GF_SPARK = 32,                /* throws five sparks where it lands (12V Charger) */
  GF_BEAM = 64,                 /* a straight line, no gravity (Yagi Beam) */
  GF_DIRT = 128                 /* piles dirt up instead of digging (Coax Spool) */
};

/* An item in the shop. Weapons: r blast radius, dmg damage at the centre (or to a station it
   hits square), cap the launch speed at full power (% of normal), gr gravity in quarters, fl
   GF_* flags. Shields (12-13): r the radius they bend shots in, dmg their energy, cap the share
   of blast damage they take (%), gr how hard they push. qty: how many a purchase gives (0 for
   QRP Shot: always in stock). */
typedef struct {
  char name[13];
  uint8_t price, qty, r, dmg, cap, gr, fl;
  char info[20];
} gw_item_t;

#define GW_SPARK_R 2
#define GW_SPARK_DMG 20

static const gw_item_t GW_ITEMS[GW_NI] = {
    {"QRP Shot", 0, 0, 3, 35, 100, 4, 0, "5 W, always free"},
    {"Kilowatt", 60, 1, 8, 80, 100, 4, 0, "QRO: big blast"},
    {"FCC Violator", 35, 2, 11, 40, 55, 4, 0, "splatter, short hop"},
    {"12V Charger", 45, 2, 3, 30, 100, 4, GF_SPARK, "sparks fly"},
    {"CW Strike", 30, 3, 2, 100, 100, 4, GF_NOWIND, "no wind, precise"},
    {"Beverage", 40, 2, 6, 65, 100, 4, GF_CEIL, "strong, stays low"},
    {"The Lid", 15, 3, 4, 45, 100, 4, GF_WANDER, "wanders around"},
    {"Skip Zone", 30, 3, 4, 45, 100, 4, GF_BOUNCE, "skips off ground"},
    {"Repeater", 55, 1, 4, 40, 100, 4, GF_SPLIT, "kerchunk: splits 3"},
    {"Yagi Beam", 35, 2, 3, 55, 100, 0, GF_BEAM | GF_NOWIND, "straight, no drop"},
    {"Coax Spool", 20, 2, 6, 0, 100, 4, GF_DIRT, "piles up dirt"},
    {"Tropo Duct", 25, 3, 4, 45, 100, 2, 0, "half gravity: far"},
    {"Ground Rod", 30, 1, 9, 60, 50, 1, 0, "shield: soaks half"},
    {"Faraday Cage", 70, 1, 12, 100, 100, 3, 0, "shield: bends shots"},
};

/* sin of 0, 6, .. 96 degrees, Q12 (gw_sin interpolates between them) */
static const uint16_t GW_SIN6[17] = {0,    428,  852,  1266, 1666, 2048, 2408, 2741, 3044,
                                     3314, 3547, 3742, 3896, 4006, 4074, 4096, 4074};

/* midpoint displacement: the random swing at each halving of the step, 128 down to 2 */
static const uint8_t GW_AMP[7] = {12, 10, 7, 4, 2, 0, 0};

typedef struct {
  uint8_t x, y;                 /* the mast's column; the dirt row the station stands on */
  uint8_t hp, sh, she;          /* shield item (0: none) and its energy left */
  uint8_t wins, dealt;          /* rounds won; damage dealt this round (dollars at its end) */
  uint8_t pad;
  uint16_t money;
  uint8_t inv[GW_NI];           /* items held (QRP Shot: unlimited, whatever this says) */
  uint8_t ang, pow, wpn;        /* the aim (last: gw_hash leaves it out, it moves while aiming):
                                   angle 0 (right) .. 180 (left), power 0..100, the item */
} gw_pl_t;

typedef struct {
  int32_t x, y, vx, vy;         /* Q16 columns, Q17 rows (GW_YS), and those a step */
  uint16_t age;
  uint8_t live, own, r, dmg, fl, gr, bnc, split;
} gw_shot_t;

typedef struct {
  uint32_t rng;
  uint8_t top[GW_W + 1];        /* (the 129th entry only while the terrain is made) */
  uint8_t round, rounds;        /* the round being played (1..), and how many a game has */
  uint8_t mover, first, turn;   /* whose turn; who starts the next round; turns this round */
  uint8_t over;                 /* 0 playing, 1 the round is over, 2 the game is over */
  uint8_t winner;               /* the last round's: 0 / 1, 2 a draw */
  int8_t wind;                  /* -GW_WMAX (to the left) .. GW_WMAX */
  uint8_t nb;                   /* blasts this step */
  uint8_t bx[GW_NB], by[GW_NB], br[GW_NB];
  gw_pl_t p[2];
  gw_shot_t s[GW_NS];
} gw_t;

/* (a struct assignment would call memcpy, which a module doesn't have) */
static void gw_copy(void *to, const void *from, unsigned n) {
  uint8_t *d = (uint8_t *)to;
  const volatile uint8_t *s = (const volatile uint8_t *)from;
  while (n--) *d++ = *s++;
}

static uint32_t gw_udiv(uint32_t n, uint32_t d) {
  uint32_t q = 0, bit = 1;
  if (!d) return 0;
  while (d < n && !(d & 0x80000000u)) d <<= 1, bit <<= 1;
  while (bit) {
    if (n >= d) n -= d, q |= bit;
    d >>= 1, bit >>= 1;
  }
  return q;
}

static uint32_t gw_isqrt(uint32_t v) {
  uint32_t r = 0, b = 1u << 30;
  while (b > v) b >>= 2;
  while (b) {
    if (v >= r + b) v -= r + b, r = (r >> 1) + b;
    else r >>= 1;
    b >>= 2;
  }
  return r;
}

static uint32_t gw_rand(gw_t *g) {
  uint32_t x = g->rng;
  x ^= x << 13;
  x ^= x >> 17;
  x ^= x << 5;
  return g->rng = x;
}

/* 0 .. n-1 (n below 65536) */
static int gw_rn(gw_t *g, uint32_t n) { return (int)(((gw_rand(g) >> 16) * n) >> 16); }

/* sin of 0..180 degrees and cos of 0..180, Q12: linear between 6-degree points (a * 43 >> 8 is
   a / 6 for 0..90) */
static int gw_sin(int a) {
  int i, f;
  if (a > 90) a = 180 - a;
  i = (a * 43) >> 8;
  f = a - 6 * i;
  return GW_SIN6[i] + (((GW_SIN6[i + 1] - GW_SIN6[i]) * f * 683) >> 12);
}
static int gw_cos(int a) { return a <= 90 ? gw_sin(90 - a) : -gw_sin(a - 90); }

static int gw_clamp(int v, int lo, int hi) { return v < lo ? lo : v > hi ? hi : v; }

/* the highest dirt under a station's footprint */
static int gw_ground(const gw_t *g, int x) {
  int i, y = GW_BED;
  for (i = x - 2; i <= x + 2; i++)
    if (g->top[i] < y) y = g->top[i];
  return y;
}

static void gw_new_game(gw_t *g, uint32_t seed, int rounds) {
  uint8_t *z = (uint8_t *)g;
  unsigned i;
  for (i = 0; i < sizeof *g; i++) z[i] = 0;
  g->rng = seed ? seed : 0x6B5A;
  g->rounds = (uint8_t)rounds;
  g->p[0].money = GW_START;
  g->p[1].money = GW_START;
}

static void gw_new_round(gw_t *g) {
  uint8_t *t = g->top;
  int step, k, i, j;
  t[0] = (uint8_t)(11 + gw_rn(g, 15));
  t[GW_W] = (uint8_t)(11 + gw_rn(g, 15));
  for (step = GW_W, k = 0; step > 1; step >>= 1, k++) {
    int a = GW_AMP[k];
    for (i = step >> 1; i < GW_W; i += step) {
      int v = (t[i - (step >> 1)] + t[i + (step >> 1)]) >> 1;
      t[i] = (uint8_t)gw_clamp(v + gw_rn(g, (uint32_t)(2 * a + 1)) - a, 9, 27);
    }
  }
  for (j = 0; j < 2; j++) {
    gw_pl_t *p = &g->p[j];
    p->x = (uint8_t)(j ? 92 + gw_rn(g, 28) : 8 + gw_rn(g, 28));
    for (i = p->x - 3; i <= p->x + 3; i++) t[i] = t[p->x];     /* a flat pad */
    p->y = t[p->x];
    p->hp = GW_HP;
    p->ang = (uint8_t)(j ? 135 : 45);
    p->pow = 60;
    p->wpn = 0;
    p->she = p->sh ? GW_ITEMS[p->sh].dmg : 0;
    p->dealt = 0;
  }
  g->wind = (int8_t)(gw_rn(g, 2 * GW_WMAX + 1) - GW_WMAX);
  g->round++;
  g->mover = g->first;
  g->first ^= 1;
  g->turn = 0;
  g->over = 0;
  g->nb = 0;
  for (i = 0; i < GW_NS; i++) g->s[i].live = 0;
}

/* buy one lot of item i for station j: 0, or -1 (money, or a second shield) */
static int gw_buy(gw_t *g, int j, int i) {
  gw_pl_t *p = &g->p[j];
  const gw_item_t *it = &GW_ITEMS[i];
  if (i <= 0 || i >= GW_NI || p->money < it->price) return -1;
  if (i >= GW_NWPN) {
    if (p->sh) return -1;
    p->sh = (uint8_t)i;
  } else {
    if (p->inv[i] + it->qty > 99) return -1;
    p->inv[i] = (uint8_t)(p->inv[i] + it->qty);
  }
  p->money = (uint16_t)(p->money - it->price);
  return 0;
}

/* a check word over everything the two radios must agree on */
static uint32_t gw_hash(const gw_t *g) {
  uint32_t h = 2166136261u;
  int i, j;
  for (i = 0; i < GW_W; i++) h = (h ^ g->top[i]) * 16777619u;
  for (j = 0; j < 2; j++) {
    const uint8_t *b = (const uint8_t *)&g->p[j];
    int n = (int)(&g->p[j].ang - b);      /* (not the aim) */
    for (i = 0; i < n; i++) h = (h ^ b[i]) * 16777619u;
  }
  h = (h ^ g->rng) * 16777619u;
  h = (h ^ (uint8_t)g->wind) * 16777619u;
  h = (h ^ (uint32_t)(g->round << 8 | g->turn)) * 16777619u;
  return h;
}

static gw_shot_t *gw_spawn(gw_t *g, const gw_shot_t *from) {
  int i;
  for (i = 0; i < GW_NS; i++)
    if (!g->s[i].live) {
      gw_copy(&g->s[i], from, sizeof *from);
      return &g->s[i];
    }
  return 0;
}

/* The mover fires item w at angle a (0..180), power pw (0..100). An item it doesn't hold
   becomes QRP Shot. Then gw_step until it says 0, and gw_end_turn. Returns the item fired. */
static int gw_fire(gw_t *g, int a, int pw, int w) {
  gw_pl_t *p = &g->p[g->mover];
  const gw_item_t *it;
  gw_shot_t s;
  int32_t v;
  uint8_t *z = (uint8_t *)&s;
  unsigned i;
  a = gw_clamp(a, 0, 180);
  pw = gw_clamp(pw, 0, 100);
  if (w <= 0 || w >= GW_NWPN || !p->inv[w]) w = 0;
  else p->inv[w]--;
  p->ang = (uint8_t)a;
  p->pow = (uint8_t)pw;
  it = &GW_ITEMS[w];
  for (i = 0; i < sizeof s; i++) z[i] = 0;
  s.live = 1;
  s.own = g->mover;
  s.r = it->r;
  s.dmg = it->dmg;
  s.fl = it->fl;
  s.gr = it->gr;
  s.bnc = (it->fl & GF_BOUNCE) ? 1 : 0;
  s.x = (int32_t)p->x << 16 | 0x8000;
  s.y = (int32_t)(p->y - 7) << GW_YS | 0x8000;
  if (it->fl & GF_BEAM) v = 3 << 16;
  else v = (int32_t)((uint32_t)(pw * it->cap * 655) >> 16) * GW_VK;
  s.vx = (v * gw_cos(a)) >> 12;
  s.vy = -((v * gw_sin(a)) >> 12);
  g->nb = 0;
  gw_spawn(g, &s);
  return w;
}

static void gw_hurt(gw_t *g, int j, int d, int own) {
  gw_pl_t *p = &g->p[j];
  if (p->she) {
    int a = (int)((uint32_t)(d * GW_ITEMS[p->sh].cap * 655) >> 16);
    if (a > p->she) a = p->she;
    p->she = (uint8_t)(p->she - a);
    d -= a;
  }
  if (d > p->hp) d = p->hp;
  p->hp = (uint8_t)(p->hp - d);
  if (j != own) g->p[own].dealt = (uint8_t)gw_clamp(g->p[own].dealt + d, 0, 255);
}

/* shot s goes off at (px, py); hit: the station it struck square, or -1 */
static void gw_blast(gw_t *g, gw_shot_t *s, int px, int py, int hit) {
  int r = s->r, dx, j;
  if (g->nb < GW_NB) {
    g->bx[g->nb] = (uint8_t)gw_clamp(px, 0, GW_W - 1);
    g->by[g->nb] = (uint8_t)gw_clamp(py, 0, GW_H - 1);
    g->br[g->nb++] = (uint8_t)r;
  }
  for (dx = -r; dx <= r; dx++) {
    int x = px + dx, h, t;
    if (x < 0 || x >= GW_W) continue;
    h = (int)gw_isqrt((uint32_t)(r * r - dx * dx));
    t = g->top[x];
    if (s->fl & GF_DIRT) {
      g->top[x] = (uint8_t)gw_clamp(t - h, 2, GW_BED);
    } else {
      int lo = py - h, hi = py + h, n;
      if (hi > GW_BED - 1) hi = GW_BED - 1;
      if (lo < t) lo = t;
      n = hi - lo + 1;
      if (n > 0) g->top[x] = (uint8_t)(t + n);
    }
  }
  if (s->dmg)
    for (j = 0; j < 2; j++) {
      gw_pl_t *p = &g->p[j];
      int d;
      if (j == hit) d = s->dmg;
      else {
        int ex = px - p->x, ey = py - (p->y - 3), R = r + 3;
        int dist = (int)gw_isqrt((uint32_t)(ex * ex + ey * ey));
        if (dist >= R) continue;
        d = (int)gw_udiv((uint32_t)(s->dmg * (R - dist)), (uint32_t)R);
      }
      gw_hurt(g, j, d, s->own);
    }
  if (s->fl & GF_SPARK) {
    gw_shot_t k;
    int i;
    gw_copy(&k, s, sizeof k);
    k.fl = 0;
    k.r = GW_SPARK_R;
    k.dmg = GW_SPARK_DMG;
    k.gr = 4;
    k.bnc = 0;
    k.age = 0;
    k.y = (int32_t)(py - 2) << GW_YS | 0x8000;
    k.x = (int32_t)px << 16 | 0x8000;
    for (i = 0; i < 5; i++) {
      k.vx = gw_rn(g, 105000) - 52500;
      k.vy = -(26000 + gw_rn(g, 52000));
      gw_spawn(g, &k);
    }
  }
}

/* is pixel (px, py) inside station j's shack and mast? */
static int gw_in_station(const gw_t *g, int j, int px, int py) {
  const gw_pl_t *p = &g->p[j];
  return px >= p->x - 3 && px <= p->x + 3 && py >= p->y - 6 && py < p->y;
}

static void gw_move(gw_t *g, gw_shot_t *s) {
  int j, px, py;
  int32_t ox = s->x, oy = s->y, ovy = s->vy;
  s->age++;
  s->vy += GW_G * s->gr;
  if (!(s->fl & GF_NOWIND)) s->vx += GW_WK * g->wind;
  if (s->fl & GF_WANDER) {
    s->vx += gw_rn(g, 8001) - 4000;
    s->vy += gw_rn(g, 4001) - 2000;
  }
  for (j = 0; j < 2; j++) {               /* shields bend shots that come near (not their own) */
    gw_pl_t *p = &g->p[j];
    const gw_item_t *it = &GW_ITEMS[p->sh];
    int ex, ey, d2, k;
    if (!p->she || j == s->own) continue;
    ex = (s->x >> 16) - p->x;
    ey = (s->y >> GW_YS) - (p->y - 3);
    d2 = ex * ex + ey * ey;
    if (d2 >= it->r * it->r) continue;
    if (d2 < 4) d2 = 4;
    k = (int)gw_udiv((uint32_t)(it->gr * GW_SHIELD_K), (uint32_t)d2);
    s->vx += k * ex;
    s->vy += k * ey;
    p->she--;
  }
  s->x += s->vx;
  s->y += s->vy;
  if ((s->fl & GF_SPLIT) && !s->split && ovy < 0 && s->vy >= 0) {
    gw_shot_t k;
    gw_copy(&k, s, sizeof k);
    s->split = k.split = 1;
    k.vx = s->vx - 19660;
    gw_spawn(g, &k);
    k.vx = s->vx + 19660;
    gw_spawn(g, &k);
  }
  if ((s->fl & GF_CEIL) && s->y < (GW_CEIL << GW_YS)) {
    s->y = GW_CEIL << GW_YS;
    if (s->vy < 0) s->vy = 0;
  }
  px = s->x >> 16;
  py = s->y >> GW_YS;
  if (px < 0 || px >= GW_W || s->age > GW_MAX_AGE || ((s->fl & GF_BEAM) && s->age > GW_BEAM_STEPS)) {
    s->live = 0;                          /* gone off the field, or fizzled */
    return;
  }
  if (py >= GW_BED) {
    s->live = 0;
    gw_blast(g, s, px, GW_BED, -1);
    return;
  }
  for (j = 0; j < 2; j++)
    if ((j != s->own || s->age > 4) && gw_in_station(g, j, px, py)) {
      s->live = 0;
      gw_blast(g, s, px, py, j);
      return;
    }
  if (py >= 0 && py >= g->top[px]) {
    if (s->bnc) {                         /* skip: back out of the dirt, up at 5/8 the speed */
      s->bnc--;
      s->x = ox;
      s->y = oy;
      s->vy = -((s->vy * 5) >> 3);
      s->vx = (s->vx * 3) >> 2;
      return;
    }
    s->live = 0;
    gw_blast(g, s, px, py, -1);
  }
}

/* one step of every shot in the air: the shots still flying (0: the shot is over) */
static int gw_step(gw_t *g) {
  int i, n = 0;
  g->nb = 0;
  for (i = 0; i < GW_NS; i++)
    if (g->s[i].live) gw_move(g, &g->s[i]);
  for (i = 0; i < GW_NS; i++) n += g->s[i].live;
  return n;
}

/* After the shot: stations settle (a fall of more than a pixel costs 2 HP a pixel), the round
   ends if a station is out, else the turn passes and the wind drifts. Returns g->over. */
static int gw_end_turn(gw_t *g) {
  int j, dead = 0;
  for (j = 0; j < 2; j++) {
    gw_pl_t *p = &g->p[j];
    int y = gw_ground(g, p->x), fall = y - p->y;
    if (fall > 1) gw_hurt(g, j, 2 * (fall - 1), j);
    p->y = (uint8_t)y;
    if (!p->hp) dead |= 1 << j;
  }
  if (dead) {
    g->winner = dead == 3 ? 2 : dead == 1 ? 1 : 0;
    for (j = 0; j < 2; j++) {
      gw_pl_t *p = &g->p[j];
      p->money = (uint16_t)(p->money + GW_ROUND_PAY + p->dealt + (g->winner == j ? GW_WIN_PAY : 0));
      if (g->winner == j) p->wins++;
      p->sh = 0;
      p->she = 0;
    }
    g->over = (g->round >= g->rounds || 2 * g->p[0].wins > g->rounds || 2 * g->p[1].wins > g->rounds) ? 2 : 1;
    return g->over;
  }
  g->mover ^= 1;
  g->turn++;
  g->wind = (int8_t)gw_clamp(g->wind + gw_rn(g, 5) - 2, -GW_WMAX, GW_WMAX);
  return 0;
}

#endif
