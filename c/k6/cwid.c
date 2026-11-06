/* cwid: Morse ID decoder for the K6 (cwid.h). SPDX-License-Identifier: MIT */
#include "cwid.h"

/* Morse by code: start at 1, then 0 for a dot and 1 for a dash, element by element
   (E = 2, T = 3, A = 5, ...). Codes of up to 6 elements; '~' unused. */
static const char cw_tab[] =
    "~~ETIANMSURWDKGOHVF~L~PJBXCYZQ~~54~3~~~2~~+~~~~16=/~~~~~7~~~8~90"
    "~~~~~~~~~~~~?~~~~~~~~.~~~~~~~~~~~-~~~~~~~~~~~~~~~~~,~~~~~~~~~~~~";

uint32_t cw_div(uint32_t n, uint32_t d) {
  uint32_t q = 0, bit = 1;
  if (!d) return 0;
  while (d < n && !(d & 0x80000000u)) { d <<= 1; bit <<= 1; }
  while (bit) {
    if (n >= d) { n -= d; q |= bit; }
    d >>= 1; bit >>= 1;
  }
  return q;
}

char *cw_utoa(char *p, uint32_t v) {
  static const uint32_t pw[] = {1000000000u, 100000000u, 10000000u, 1000000u, 100000u,
                                10000u, 1000u, 100u, 10u, 1u};
  int i, started = 0;
  for (i = 0; i < 10; i++) {
    char dch = '0';
    while (v >= pw[i]) { v -= pw[i]; dch++; }
    if (dch != '0' || started || i == 9) { *p++ = dch; started = 1; }
  }
  return p;
}

int cw_db(uint32_t v) {
  /* 20 log10(v) = 6.0206 log2(v): the top bit's position plus 8 bits of fraction, corrected
     by the log2(1 + x) - x bulge (0.343 x (1 - x), within 0.01 octave) */
  int p = 31, f;
  if (v <= 1) return 0;
  while (!(v & (1u << p))) p--;
  f = p >= 8 ? (int)((v >> (p - 8)) & 255) : (int)((v << (8 - p)) & 255);
  f += (f * (256 - f) * 88) >> 16;
  /* (p + f/256) octaves x 6.0206 dB, in 1/16 dB: x 0.37629 = 385/1024 */
  return (int)(((((uint32_t)p << 8) + (uint32_t)f) * 385 + 8192) >> 14);
}

void cw_lvl_init(cw_lvl_t *l) {
  uint8_t *b = (uint8_t *)l;
  unsigned i;
  for (i = 0; i < sizeof *l; i++) b[i] = 0;
}

int cw_lvl_contrast(const cw_lvl_t *l) { return (int)((l->hi - l->lo) >> 8); }

static int32_t iabs(int32_t x) { return x < 0 ? -x : x; }

int cw_lvl(cw_lvl_t *l, int db, uint32_t dt) {
  int32_t x, mid, h, c;
  int32_t k = (int32_t)(dt > 64 ? 64 : dt);
  if (!l->init) { l->lo = l->hi = l->sm = (int32_t)db << 8; l->dev = 0; l->init = 1; }
  /* smoothed a little first (tau 4 ms): the register steps every few ms with noise on it */
  l->sm += ((((int32_t)db << 8) - l->sm) * (k > 4 ? 4 : k)) >> 2;
  x = l->sm;
  /* two levels, key down and key up: a sample above the middle moves the upper one (tau 2^CW_LVL_TAU ms)
     and pulls the lower one up slowly (4 s), and the other way round; dev is the samples'
     mean distance from their own level (tau 128 ms), the noise the keying must stand out of */
  mid = (l->lo + l->hi) >> 1;
  if (x >= mid) {
    l->hi += ((x - l->hi) * k) >> CW_LVL_TAU;
    l->lo += ((l->hi - l->lo) * k) >> 12;
    l->dev += ((iabs(x - l->hi) - l->dev) * k) >> 7;
  } else {
    l->lo += ((x - l->lo) * k) >> CW_LVL_TAU;
    l->hi += ((l->lo - l->hi) * k) >> 12;
    l->dev += ((iabs(x - l->lo) - l->dev) * k) >> 7;
  }
  /* the decision is for the sample CW_LVL_LOOK polls ago, against the levels as they are now:
     the first dot of an ID is keyed with the key-down level it set up itself */
  {
    int32_t past = l->ring[l->ri];
    l->ring[l->ri] = (int16_t)(x >> 4);
    l->ri = (uint8_t)((l->ri + 1) & (CW_LVL_LOOK - 1));
    if (l->fill < CW_LVL_LOOK) { l->fill++; return 0; }
    x = past << 4;
  }
  c = l->hi - l->lo;
  if (c < (CW_MIN_CONTRAST << 8) || c < CW_NOISE_X * l->dev + (CW_NOISE_DB << 8)) { l->on = 0; return 0; }
  mid = (l->lo + l->hi) >> 1;
  h = c >> 3;
  if (l->on) { if (x < mid - h) l->on = 0; }
  else if (x > mid + h) l->on = 1;
  return l->on;
}

void cw_init(cw_t *c) {
  uint8_t *b = (uint8_t *)c;
  unsigned i;
  for (i = 0; i < sizeof *c; i++) b[i] = 0;
}

/* mean of the values in v[first], v[first + 2], ... (n values held) within [lo, hi) */
static uint32_t mean_in(const uint8_t *v, int first, int n, uint32_t lo, uint32_t hi, int *cnt) {
  uint32_t s = 0;
  int i, k = 0;
  for (i = first; i < n; i += 2)
    if (v[i] >= lo && v[i] < hi) { s += v[i]; k++; }
  *cnt = k;
  return k ? cw_div(s + ((uint32_t)k >> 1), (uint32_t)k) : 0;
}

/* two-means on v[first], v[first + 2], ... in [floor, ceil): returns the split point and the
   two means */
static uint32_t two_means(const uint8_t *v, int first, int n, uint32_t floor, uint32_t ceil,
                          uint32_t *lo, uint32_t *hi) {
  uint32_t a = 0xFFFF, b = 0, thr = 0;
  int i, it;
  for (i = first; i < n; i += 2)
    if (v[i] >= floor && v[i] < ceil) { if (v[i] < a) a = v[i]; if (v[i] > b) b = v[i]; }
  if (b < a) { *lo = *hi = 0; return 0; }
  for (it = 0; it < 8; it++) {
    uint32_t sa = 0, sb = 0, ka = 0, kb = 0;
    thr = (a + b + 1) >> 1;
    for (i = first; i < n; i += 2) {
      uint32_t x = v[i];
      if (x < floor || x >= ceil) continue;
      if (x < thr) { sa += x; ka++; } else { sb += x; kb++; }
    }
    if (!ka || !kb) break;
    sa = cw_div(sa + (ka >> 1), ka);
    sb = cw_div(sb + (kb >> 1), kb);
    if (sa == a && sb == b) break;
    a = sa; b = sb;
  }
  *lo = a; *hi = b;
  return thr;
}

/* Merge marks shorter than min (noise that got through) into the spaces around them, in place:
   d[] alternates mark, space, ..., mark (n odd). Returns the new count. */
static int drop_short(uint8_t *d, int n, uint32_t min) {
  int i, o = 0;
  uint32_t acc = 0;
  for (i = 0; i < n; i += 2) {
    if (d[i] < min) {                         /* gone: it and the space after join the gap */
      acc += d[i] + (i + 1 < n ? d[i + 1] : 0);
      continue;
    }
    if (o) { uint32_t g = d[o - 1] + acc; d[o - 1] = (uint8_t)(g > 255 ? 255 : g); }
    acc = 0;
    d[o++] = d[i];
    if (i + 1 < n) d[o++] = d[i + 1];
  }
  if (o && !(o & 1)) o--;                     /* ends on a mark */
  return o;
}

static void put_char(cw_t *c, char ch) {
  if (c->tlen < CW_TEXT - 1) c->text[c->tlen++] = ch;
  c->text[c->tlen] = 0;
}

static void decode(cw_t *c) {
  uint8_t *d = c->d;                          /* in CW_Q ms units, as everything below */
  int n = c->n, i, nel = 0;
  uint32_t dot, dash, u, sp_lo, sp_hi, word = 0xFFFF, code = 1;
  int32_t e;
  c->tlen = 0;
  c->text[0] = 0;
  c->nd = (uint16_t)n;
  if (!(n & 1)) n--;                          /* a trailing space is the separator */
  n = drop_short(c->d, n, CW_MINMARK_MS / CW_Q);
  c->nd = (uint16_t)n;
  if (n < 1) return;
  two_means(d, 0, n, 0, 0x10000, &dot, &dash);
  if (dash >= 2 * dot) {
    /* both kinds: dot = u + e, dash = 3u + e, with e the detector's lag */
    u = (dash - dot + 1) >> 1;
    e = (int32_t)dot - (int32_t)u;
  } else {
    /* one kind of mark: compare with the short spaces (inside a letter, u - e) */
    uint32_t m = (dot + dash + 1) >> 1, es;
    int ks;
    es = mean_in(d, 1, n, 0, 2 * m, &ks);
    if (!ks) { u = m; e = 0; }                /* a lone mark: call it a dot */
    else if (m >= 2 * es) {                   /* dashes: 3u + e and u - e */
      u = (m + es + 2) >> 2;
      e = (int32_t)m - 3 * (int32_t)u;
    } else {                                  /* dots: u + e and u - e */
      u = (m + es + 1) >> 1;
      e = (int32_t)m - (int32_t)u;
    }
  }
  if (u < 20 / CW_Q) u = 20 / CW_Q;           /* 60 wpm */
  if (e > (int32_t)(u >> 1)) e = (int32_t)(u >> 1);
  if (e < -(int32_t)(u >> 1)) e = -(int32_t)(u >> 1);
  c->unit = (uint16_t)(u * CW_Q);
  c->lag = (int16_t)(e * CW_Q);
  c->wpm = (uint8_t)cw_div(1200 / CW_Q + (u >> 1), u);
  /* the gaps between letters and between words: two clusters above 2 dots, if they're apart
     (measured, so short by e) */
  {
    int32_t fl = 2 * (int32_t)u - e;
    /* (spaces over 10 dots are breaks, word gaps whatever the clusters say) */
    uint32_t thr = two_means(d, 1, n, fl > 1 ? (uint32_t)fl : 1, 10 * u, &sp_lo, &sp_hi);
    if (sp_hi && sp_lo && 4 * sp_hi >= 7 * sp_lo) {
      word = thr;
      if ((int32_t)word < 4 * (int32_t)u - e) word = (uint32_t)(4 * (int32_t)u - e);
    }                                         /* one cluster: letter gaps (Farnsworth) */
  }
  for (i = 0; i < n; i++) {
    uint32_t v = d[i];
    if (!(i & 1)) {                           /* a mark */
      int32_t m = (int32_t)v - e;
      code = (code << 1) | (m >= (int32_t)(2 * u));
      nel++;
    } else {                                  /* a space, measured short by e */
      int32_t s = (int32_t)v + e;
      if (s >= (int32_t)(2 * u)) {
        put_char(c, nel > 6 ? '#' : cw_tab[code & 127] == '~' ? '*' : cw_tab[code & 127]);
        code = 1; nel = 0;
        if ((word != 0xFFFF && v >= word) || s >= (int32_t)(10 * u)) put_char(c, ' ');
      }
    }
  }
  if (nel) put_char(c, nel > 6 ? '#' : cw_tab[code & 127] == '~' ? '*' : cw_tab[code & 127]);
}

static int finish(cw_t *c, int why) {
  c->why = (uint8_t)why;
  decode(c);
  c->n = 0;
  return CW_MSG;
}

/* ms -> CW_Q ms units, saturating */
static uint8_t q8(uint32_t ms) {
  ms = (ms + CW_Q / 2) >> CW_QSHIFT;
  return (uint8_t)(ms > 255 ? 255 : ms);
}

int cw_feed(cw_t *c, uint32_t t, int keyed) {
  int r = CW_NONE;
  keyed = keyed ? 1 : 0;
  if (keyed != c->key) {
    if (!c->cand) { c->cand = 1; c->cand_t = t; }
    else if (t - c->cand_t >= CW_GLITCH_MS) {
      uint32_t te = c->cand_t, dur = te - c->edge_t;
      c->cand = 0;
      if (!c->active) {
        if (keyed) { c->active = 1; c->n = 0; c->why = CW_END; }
      } else {
        c->d[c->n++] = q8(dur);
        if (c->key && dur > CW_MAXMARK_MS) c->why = CW_LONG;
        if (!c->key && c->n >= CW_NDUR - 1) {
          int why = c->why == CW_LONG ? CW_LONG : CW_FULL;
          r = finish(c, why);
          c->why = CW_END;
        }
      }
      c->key = (uint8_t)keyed;
      c->edge_t = te;
    }
  } else {
    c->cand = 0;
  }
  if (c->active && !c->key && !c->cand && t - c->edge_t >= CW_END_MS) {
    int why = c->why;
    c->active = 0;
    if (c->n) r = finish(c, why);
  }
  return r;
}

int cw_flush(cw_t *c, uint32_t t) {
  int why = c->why;
  if (!c->active) return CW_NONE;
  if (c->key) {                               /* close the mark that's still on */
    uint32_t dur = t - c->edge_t;
    if (c->n < CW_NDUR) c->d[c->n++] = q8(dur);
  }
  c->active = 0;
  c->key = 0;
  c->cand = 0;
  c->edge_t = t;
  return c->n ? finish(c, why) : CW_NONE;
}

/* ---- callsigns ---- */

static int is_l(char ch) { return ch >= 'A' && ch <= 'Z'; }
static int is_d(char ch) { return ch >= '0' && ch <= '9'; }

/* a base call: prefix (1-3, a letter in it, not ending in a digit run that belongs to the
   numeral... kept simple: [prefix of 1-3 with a letter][1-3 digits][1-5 letters]) */
static int base_call(const char *s, int n) {
  int i, j, p, letters = 0;
  if (n < 3 || n > 10) return 0;
  /* the suffix: letters at the end */
  j = n;
  while (j > 0 && is_l(s[j - 1])) j--;
  if (j == n || n - j > 5) return 0;
  /* the numeral: digits before it */
  i = j;
  while (i > 0 && is_d(s[i - 1])) i--;
  if (i == j || j - i > 3) return 0;
  /* the prefix: 1-3 characters before the numeral, with a letter, letters/digits only */
  p = i;
  if (p < 1 || p > 3) return 0;
  for (i = 0; i < p; i++) {
    if (is_l(s[i])) letters++;
    else if (!is_d(s[i])) return 0;
  }
  return letters > 0;
}

static int same(const char *a, int n, const char *b) {
  int i;
  for (i = 0; i < n; i++) if (a[i] != b[i]) return 0;
  return b[n] == 0;
}

static uint8_t suffix_flag(const char *s, int n) {
  if (same(s, n, "R") || same(s, n, "RPT") || same(s, n, "RPTR")) return CW_CALL_R;
  if (same(s, n, "P") || same(s, n, "QRP")) return CW_CALL_P;
  if (same(s, n, "M") || same(s, n, "MM") || same(s, n, "AM")) return CW_CALL_M;
  return 0;
}

int cw_call(const char *text, char call[11], uint8_t *flags) {
  const char *t = text;
  *flags = 0;
  call[0] = 0;
  while (*t) {
    const char *w, *parts[4];
    int len, lens[4], np = 0, i, base = -1, n;
    while (*t == ' ') t++;
    w = t;
    while (*t && *t != ' ') t++;
    len = (int)(t - w);
    if (!len) break;
    /* split on '/' */
    {
      const char *q = w;
      while (q < t && np < 4) {
        const char *s0 = q;
        while (q < t && *q != '/') q++;
        parts[np] = s0; lens[np] = (int)(q - s0); np++;
        if (q < t) q++;
      }
    }
    for (i = 0; i < np; i++)
      if (base_call(parts[i], lens[i])) { if (base < 0 || lens[i] > lens[base]) base = i; }
    if (base < 0) continue;
    for (i = 0; i < np; i++) if (i != base) *flags |= suffix_flag(parts[i], lens[i]);
    /* the call as heard if it fits, else the base call */
    {
      const char *src = len <= 10 ? w : parts[base];
      n = len <= 10 ? len : lens[base];
      for (i = 0; i < n; i++) call[i] = src[i];
      call[n] = 0;
    }
    /* a separate R / RPT / P / M right after it */
    {
      const char *u = t, *v;
      while (*u == ' ') u++;
      v = u;
      while (*v && *v != ' ') v++;
      if (v > u) *flags |= suffix_flag(u, (int)(v - u));
    }
    return n;
  }
  return 0;
}
