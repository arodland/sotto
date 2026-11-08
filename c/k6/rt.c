/* rt: the K6 explorer's repeater table in SPI flash (rt.h has the format and the rules).
   SPDX-License-Identifier: MIT */
#include "rt.h"

#define LO(st) ((st) & 0xFFu)
#define SEC(slot) ((unsigned)(slot) >> 6)
#define RT_ST_KILLED 0x00u          /* low byte: mount cleared a slot it couldn't read */

static uint32_t addr(const rt_t *t, int slot) { return t->base + ((uint32_t)slot << 6); }

static int rd(rt_t *t, int slot, rt_rec_t *r) {
  return t->fl->read(t->fl->ctx, addr(t, slot), r, RT_REC) < 0 ? RT_ERR_FLASH : 0;
}

static int state(rt_t *t, int slot, uint16_t *st) {
  uint8_t b[2];
  if (t->fl->read(t->fl->ctx, addr(t, slot), b, 2) < 0) return RT_ERR_FLASH;
  *st = (uint16_t)(b[0] | b[1] << 8);
  return 0;
}

static int set_state(rt_t *t, int slot, uint16_t st) {
  uint8_t b[2];
  b[0] = (uint8_t)st, b[1] = (uint8_t)(st >> 8);
  return t->fl->write(t->fl->ctx, addr(t, slot), b, 2) < 0 ? RT_ERR_FLASH : 0;
}

uint32_t rt_crc(const rt_rec_t *r) {
  const uint8_t *p = (const uint8_t *)r + 2;
  uint32_t c = 0xFFFFFFFFu;
  int i, k;
  for (i = 2; i < 60; i++, p++) {
    c ^= *p;
    for (k = 0; k < 8; k++) c = (c >> 1) ^ (0xEDB88320u & (0u - (c & 1)));
  }
  return ~c;
}

/* a record copied by bytes (a struct assignment would call memcpy, which a module lacks) */
static void cp(rt_rec_t *to, const rt_rec_t *from) {
  uint8_t *d = (uint8_t *)to;
  const uint8_t *p = (const uint8_t *)from;
  int i;
  for (i = 0; i < RT_REC; i++) d[i] = p[i];
}

static int good(const rt_rec_t *r) { return LO(r->state) == RT_ST_VALID && r->crc == rt_crc(r); }

static int blank(const rt_rec_t *r) {
  const uint8_t *p = (const uint8_t *)r;
  int i;
  for (i = 0; i < RT_REC; i++)
    if (p[i] != 0xFF) return 0;
  return 1;
}

/* a state rt writes (anything else in the region is someone else's data) */
static int known(uint16_t st) {
  unsigned lo = LO(st), hi = st >> 8;
  if (lo == RT_ST_KILLED) return st == 0;
  if (lo != RT_ST_FREE && lo != RT_ST_WRITING && lo != RT_ST_VALID && lo != RT_ST_REPLACED &&
      lo != RT_ST_DELETED)
    return 0;
  return hi == 0xFF || (hi & 0xF0) == 0xE0 || (hi & 0xF0) == 0xC0 || (hi & 0xF0) == 0x80;
}

static void count(rt_t *t) {
  int s;
  uint16_t st;
  t->live = t->dead = t->free = 0;
  for (s = 0; s < RT_SLOTS; s++) {
    if (state(t, s, &st)) continue;
    if (LO(st) == RT_ST_VALID) t->live++;
    else if (LO(st) == RT_ST_FREE) { if (SEC(s) != t->spare) t->free++; }
    else t->dead++;
  }
}

/* is every slot of sector k free (erased)? */
static int sector_free(rt_t *t, unsigned k) {
  rt_rec_t r;
  int s;
  for (s = (int)k * RT_SLOTS_PER_SECTOR; s < (int)(k + 1) * RT_SLOTS_PER_SECTOR; s++)
    if (rd(t, s, &r) || !blank(&r)) return 0;
  return 1;
}

int rt_mount(rt_t *t, const rt_flash_t *fl, uint32_t base, int force) {
  rt_rec_t r;
  int s, last = -1, ngood = 0, foreign = 0, kills = 0, ours = 0;
  uint16_t pend = 0, comm = 0;        /* sectors a compaction was copying from / had copied */
  unsigned k;
  t->fl = fl, t->base = base, t->max_id = 0, t->wseq = 0, t->spare = 0xFF, t->gcs = 0, t->repairs = 0;
  for (s = 0; s < RT_SLOTS; s++) {
    if (rd(t, s, &r)) return RT_ERR_FLASH;
    if (!known(r.state) || (LO(r.state) == RT_ST_FREE && !blank(&r)) ||
        (LO(r.state) == RT_ST_VALID && !good(&r))) {
      foreign++;
      continue;
    }
    if (r.state == 0) kills++;
    else if (LO(r.state) != RT_ST_FREE) ours++;
    /* ids of deleted and replaced entries aren't handed out again either */
    if (LO(r.state) != RT_ST_FREE && r.crc == rt_crc(&r) && r.id > t->max_id && r.id != 0xFFFF)
      t->max_id = r.id;
    if (!good(&r)) continue;
    ngood++;
    if (last < 0 || (int32_t)(r.wseq - t->wseq) > 0) t->wseq = r.wseq, last = s;
    if ((r.state >> 12) == 0xE) pend |= (uint16_t)(1u << ((r.state >> 8) & 15));
    if ((r.state >> 12) == 0xC) comm |= (uint16_t)(1u << ((r.state >> 8) & 15));
  }
  /* nothing of ours, and something else (zeros look like killed slots: only ours if some other
     slot shows rt was here) */
  if (!ngood && !ours && (foreign || kills) && !force) return RT_ERR_NOT_BLANK;
  /* a compaction cut short. Copies marked E0|V: the copy may be partial and V is untouched, so
     the copies go. Any copy marked C0|V: the copy was complete, so V (maybe half erased) is
     erased again and the copies become the records (80|V). */
  for (k = 0; k < RT_SECTORS; k++) {
    unsigned hi = 0;
    if (!((pend | comm) & (1u << k))) continue;
    if (comm & (1u << k)) {
      for (s = 0; s < RT_SLOTS; s++)
        if (!rd(t, s, &r) && good(&r) && (r.state >> 8) == (0xE0u | k) &&
            set_state(t, s, (uint16_t)(r.state & ~0x2000u)))
          return RT_ERR_FLASH;
      if (fl->erase(fl->ctx, base + (k << 12)) < 0) return RT_ERR_FLASH;
      hi = 0xC0u | k;
    } else {
      hi = 0xE0u | k;
    }
    for (s = 0; s < RT_SLOTS; s++) {
      if (rd(t, s, &r) || !good(&r) || (r.state >> 8) != hi) continue;
      if (set_state(t, s, (comm & (1u << k)) ? (uint16_t)(r.state & ~0x4000u)
                                              : (uint16_t)((r.state & 0xFF00u) | RT_ST_DELETED)))
        return RT_ERR_FLASH;
    }
    t->repairs++;
  }
  /* the newest record again: the repairs above may have removed it */
  for (last = -1, s = 0; s < RT_SLOTS; s++)
    if (!rd(t, s, &r) && good(&r) && (last < 0 || (int32_t)(r.wseq - t->wseq) > 0)) t->wseq = r.wseq, last = s;
  /* the last edit cut short: older valid versions of the newest record's entry are replaced */
  if (last >= 0 && !rd(t, last, &r) && good(&r)) {
    uint16_t id = r.id;
    uint32_t w = r.wseq;
    for (s = 0; s < RT_SLOTS; s++) {
      if (s == last || rd(t, s, &r) || !good(&r) || r.id != id || r.wseq == w) continue;
      if (set_state(t, s, (uint16_t)((r.state & 0xFF00u) | RT_ST_REPLACED))) return RT_ERR_FLASH;
      t->repairs++;
    }
  }
  /* slots that aren't ours (or were half-erased) are killed, so they count as dead */
  if (foreign)
    for (s = 0; s < RT_SLOTS; s++) {
      if (rd(t, s, &r)) return RT_ERR_FLASH;
      if (!known(r.state) || (LO(r.state) == RT_ST_FREE && !blank(&r)) ||
          (LO(r.state) == RT_ST_VALID && !good(&r)))
        if (set_state(t, s, 0)) return RT_ERR_FLASH;
    }
  /* the spare: the last sector that's wholly erased; else one with nothing live, erased now */
  for (k = RT_SECTORS; k-- > 0;)
    if (sector_free(t, k)) { t->spare = (uint8_t)k; break; }
  if (t->spare == 0xFF)
    for (k = RT_SECTORS; k-- > 0;) {
      int live = 0;
      for (s = (int)k * RT_SLOTS_PER_SECTOR; s < (int)(k + 1) * RT_SLOTS_PER_SECTOR; s++)
        if (!rd(t, s, &r) && good(&r)) live = 1;
      if (!live) {
        if (fl->erase(fl->ctx, base + (k << 12)) < 0) return RT_ERR_FLASH;
        t->spare = (uint8_t)k;
        break;
      }
    }
  count(t);
  return t->live;
}

int rt_find(rt_t *t, uint16_t id, uint32_t rx_hz, uint32_t tol, rt_rec_t *out) {
  rt_rec_t r;
  int s, best = RT_ERR_NONE;
  uint32_t bd = 0, bw = 0;
  uint16_t st;
  for (s = 0; s < RT_SLOTS; s++) {
    uint32_t d;
    if (state(t, s, &st) || LO(st) != RT_ST_VALID || rd(t, s, &r) || !good(&r)) continue;
    if (id) {
      if (r.id != id) continue;
      d = 0;
    } else {
      d = r.rx_hz > rx_hz ? r.rx_hz - rx_hz : rx_hz - r.rx_hz;
      if (d > tol) continue;
    }
    if (best < 0 || d < bd || (d == bd && (int32_t)(r.wseq - bw) > 0)) {
      best = s, bd = d, bw = r.wseq;
      if (out) cp(out, &r);
    }
  }
  return best;
}

int rt_next(rt_t *t, int slot, rt_rec_t *out) {
  rt_rec_t r;
  uint16_t st;
  int s;
  for (s = slot + 1; s < RT_SLOTS; s++) {
    if (state(t, s, &st) || LO(st) != RT_ST_VALID || rd(t, s, &r) || !good(&r)) continue;
    if (out) cp(out, &r);
    return s;
  }
  return RT_ERR_NONE;
}

/* make room: the sector with the most dead slots, its live records copied to the spare */
static int compact(rt_t *t) {
  rt_rec_t r;
  int s, d, most = 0, to;
  unsigned k, v = 0xFF;
  uint16_t st;
  if (t->spare == 0xFF) return RT_ERR_FULL;
  for (k = 0; k < RT_SECTORS; k++) {
    if (k == t->spare) continue;
    for (d = 0, s = (int)k * RT_SLOTS_PER_SECTOR; s < (int)(k + 1) * RT_SLOTS_PER_SECTOR; s++)
      if (!state(t, s, &st) && LO(st) != RT_ST_VALID && LO(st) != RT_ST_FREE) d++;
    if (d > most) most = d, v = k;
  }
  if (v == 0xFF) return RT_ERR_FULL;
  to = t->spare * RT_SLOTS_PER_SECTOR;
  for (s = (int)v * RT_SLOTS_PER_SECTOR; s < (int)(v + 1) * RT_SLOTS_PER_SECTOR; s++) {
    if (rd(t, s, &r) || !good(&r)) continue;
    /* marked as a copy from the first write (E0|V, being written), then the body, then valid:
       a cut leaves a dead slot or a marked copy, which rt_mount sorts out */
    if (set_state(t, to, (uint16_t)((RT_ST_COPY | v << 8) | RT_ST_WRITING)) ||
        t->fl->write(t->fl->ctx, addr(t, to) + 2, (uint8_t *)&r + 2, RT_REC - 2) < 0 ||
        set_state(t, to, (uint16_t)((RT_ST_COPY | v << 8) | RT_ST_VALID)))
      return RT_ERR_FLASH;
    to++;
  }
  /* committed (C0|V), then V erased, then done (80|V) */
  for (s = t->spare * RT_SLOTS_PER_SECTOR; s < to; s++)
    if (set_state(t, s, (uint16_t)((RT_ST_SETTLED | v << 8) | RT_ST_VALID))) return RT_ERR_FLASH;
  if (t->fl->erase(t->fl->ctx, t->base + (v << 12)) < 0) return RT_ERR_FLASH;
  for (s = t->spare * RT_SLOTS_PER_SECTOR; s < to; s++)
    if (set_state(t, s, (uint16_t)((RT_ST_DONE | v << 8) | RT_ST_VALID))) return RT_ERR_FLASH;
  t->spare = (uint8_t)v, t->gcs++;
  count(t);
  return 0;
}

static int alloc(rt_t *t) {
  int s, tries;
  uint16_t st;
  for (tries = 0; tries < 2; tries++) {
    for (s = 0; s < RT_SLOTS; s++) {
      if (SEC(s) == t->spare || state(t, s, &st)) continue;
      if (LO(st) == RT_ST_FREE && st == 0xFFFF) return s;
    }
    if ((s = compact(t)) < 0) return s;
  }
  return RT_ERR_FULL;
}

int rt_put(rt_t *t, rt_rec_t *r) {
  rt_rec_t old;
  int os = RT_ERR_NONE, s;
  if (r->id == 0xFFFF) return RT_ERR_ARG;
  if (r->id) {
    if ((os = rt_find(t, r->id, 0, 0, &old)) < 0) return RT_ERR_NONE;
    r->seq = (uint16_t)(old.seq + 1);
  } else {
    if (t->max_id >= 0xFFFE) return RT_ERR_FULL;
    r->id = (uint16_t)(t->max_id + 1);
    r->seq = 1;
  }
  if ((s = alloc(t)) < 0) return s;
  if (r->id > t->max_id) t->max_id = r->id;
  /* the compaction may have moved the old version */
  if (os >= 0 && (os = rt_find(t, r->id, 0, 0, &old)) < 0) return RT_ERR_FLASH;
  r->state = 0xFFFF, r->spare0 = 0, r->wseq = ++t->wseq;
  r->crc = rt_crc(r);
  if (set_state(t, s, 0xFFFE) || t->fl->write(t->fl->ctx, addr(t, s) + 2, (uint8_t *)r + 2, RT_REC - 2) < 0 ||
      set_state(t, s, 0xFFFC))
    return RT_ERR_FLASH;
  r->state = 0xFFFC;
  t->free--;
  if (os >= 0) {
    if (set_state(t, os, (uint16_t)((old.state & 0xFF00u) | RT_ST_REPLACED))) return RT_ERR_FLASH;
    t->dead++;
  } else {
    t->live++;
  }
  return s;
}

int rt_delete(rt_t *t, uint16_t id) {
  rt_rec_t r;
  int s = rt_find(t, id, 0, 0, &r);
  if (s < 0) return s;
  if (set_state(t, s, (uint16_t)((r.state & 0xFF00u) | RT_ST_DELETED))) return RT_ERR_FLASH;
  t->live--, t->dead++;
  return 0;
}

/* ---- the CW ID listener's results ---- */

/* tone index (tone - 400) / 60 (tonedet.h's bank) and wpm / 3 by multiplies (exact over
   400-1360 Hz, 0-99 wpm) */
uint8_t rt_cw_byte(int tone_hz, int wpm) {
  int i = tone_hz < 400 ? 0 : ((tone_hz - 400) * 1093) >> 16;
  int w = wpm < 0 ? 0 : wpm > 99 ? 33 : (wpm * 21846) >> 16;
  if (i > 15) i = 15;
  if (w > 15) w = 15;
  return (uint8_t)(i | (w ? w : 1) << 4);
}

/* the call as stored: up to 10 characters, NUL padded */
static int same_call(const char *stored, const char *call) {
  int i;
  for (i = 0; i < 10; i++) {
    if (stored[i] != call[i]) return 0;
    if (!call[i]) return 1;
  }
  return call[10] == 0;
}

int rt_cw_check(rt_t *t, uint32_t rx_hz, uint32_t tol, const char *call, rt_rec_t *out) {
  rt_rec_t r;
  if (rt_find(t, 0, rx_hz, tol, &r) < 0) return RT_CW_NO_ENTRY;
  if (out) cp(out, &r);
  if (!r.call[0]) return RT_CW_FILL;
  return same_call(r.call, call) ? RT_CW_SAME : RT_CW_ASK;
}

int rt_cw_apply(rt_t *t, uint32_t rx_hz, uint32_t tol, const char *call, uint8_t call_flags, int tone_hz,
                int wpm, int confirmed, rt_rec_t *out) {
  rt_rec_t r;
  int res = rt_cw_check(t, rx_hz, tol, call, &r), i, e;
  if (res == RT_CW_NO_ENTRY || res == RT_CW_SAME || (res == RT_CW_ASK && !confirmed)) {
    if (out && res != RT_CW_NO_ENTRY) cp(out, &r);
    return res;
  }
  for (i = 0, e = 0; i < 10; i++) {
    if (!call[i]) e = 1;
    r.call[i] = e ? 0 : call[i];
  }
  r.call_flags = (uint8_t)(call_flags | RT_C_FROM_CW);
  r.flags &= (uint16_t)~RT_F_CALL_USER;
  r.cw = rt_cw_byte(tone_hz, wpm);
  if ((e = rt_put(t, &r)) < 0) return e;
  if (out) cp(out, &r);
  return res == RT_CW_FILL ? RT_CW_FILL : RT_CW_CHANGED;
}

int rt_heard_apply(rt_t *t, uint32_t rx_hz, uint32_t tol, uint16_t tone, int dmr, rt_rec_t *out) {
  rt_rec_t r;
  int res = RT_TONE_SAME, e, write = 0;
  if (rt_find(t, 0, rx_hz, tol, &r) < 0) return RT_TONE_NO_ENTRY;
  if (tone && r.rx_tone != tone) {
    if (r.rx_tone) {
      res = RT_TONE_DIFFERS;
    } else {
      r.rx_tone = tone;
      if (!r.tx_tone) r.tx_tone = tone;
      res = RT_TONE_FILL, write = 1;
    }
  }
  if (dmr && !(r.flags & RT_F_DMR)) r.flags |= RT_F_DMR, write = 1;
  if (write && (e = rt_put(t, &r)) < 0) return e;
  if (out) cp(out, &r);
  return res;
}
