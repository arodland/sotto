/* gw_host: GROUNDWAVE's rules (c/k6/mod/gw_game.h) built for the host, for tests/test_k6_gw.py and
   the PC opponent (modem/k6gw.py). The same file the K6 runs, so the host's game stays in step
   with the radio's to the bit.

     gw_size()                    bytes in a gw_t
     gw_h_new(g, seed, rounds)    a new game;  gw_h_round(g): the next round's field
     gw_h_buy(g, j, item)         0, or -1
     gw_h_fire(g, ang, pow, item) the item fired;  gw_h_step(g): shots still flying
     gw_h_end(g)                  gw_end_turn: 0 playing, 1 round over, 2 game over
     gw_h_shot(g, angle, power, item, trace, max, n)   fire, step to the end and end the turn;
                                  trace gets the first shot's pixels (x, y pairs), up to max, and
                                  *n how many; returns the steps taken
     gw_h_hash(g)                 the check word both radios compare
     gw_h_give(g, j, item, n)     station j holds n of a weapon, or the shield (tests)
     gw_h_set_shop(g, j, sh, inv, money)   station j's shop as the other radio's SHOP packet says
   SPDX-License-Identifier: MIT */
#include "mod/gw_game.h"

int gw_size(void) { return (int)sizeof(gw_t); }
void gw_h_new(gw_t *g, uint32_t seed, int rounds) { gw_new_game(g, seed, rounds); }
void gw_h_round(gw_t *g) { gw_new_round(g); }
int gw_h_buy(gw_t *g, int j, int i) { return gw_buy(g, j, i); }
int gw_h_fire(gw_t *g, int a, int pw, int w) { return gw_fire(g, a, pw, w); }
int gw_h_step(gw_t *g) { return gw_step(g); }
int gw_h_end(gw_t *g) { return gw_end_turn(g); }
uint32_t gw_h_hash(gw_t *g) { return gw_hash(g); }
const gw_item_t *gw_h_items(void) { return GW_ITEMS; }

void gw_h_give(gw_t *g, int j, int i, int n) {
  if (i >= GW_NWPN) {
    g->p[j].sh = (uint8_t)i;
    g->p[j].she = GW_ITEMS[i].dmg;
  } else {
    g->p[j].inv[i] = (uint8_t)n;
  }
}

int gw_h_shot(gw_t *g, int a, int pw, int w, int16_t *trace, int max, int *got) {
  int n = 0, k = 0;
  gw_fire(g, a, pw, w);
  for (;;) {
    int live = gw_step(g);
    n++;
    if (trace && k < max && g->s[0].live) {
      trace[2 * k] = (int16_t)(g->s[0].x >> 16);
      trace[2 * k + 1] = (int16_t)(g->s[0].y >> GW_YS);
      k++;
    }
    if (!live) break;
  }
  gw_end_turn(g);
  if (got) *got = k;
  return n;
}

void gw_h_set_shop(gw_t *g, int j, int sh, const uint8_t *inv, int money) {
  int i;
  g->p[j].sh = (uint8_t)(sh < GW_NI ? sh : 0);
  for (i = 0; i < GW_NI; i++) g->p[j].inv[i] = inv[i];
  g->p[j].money = (uint16_t)money;
}
