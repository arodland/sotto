/* dogtx: bench test of the stuck key-up backstop (hal::txdog in the Aura patch, 2026-11-08).
   It sends the CW ID as cwkey.c does (440.850 only, clear channel, AG7EW), keys up again with
   the PA off at once, so nothing goes out, and then hangs without calling the core, the way
   the report key-up hung on 2026-11-08 16:53Z. The core's own module limit only acts on module
   calls, so only txdog can end it: tx_on(DOG_MS) gives the key-up DOG_MS + 10 s, and the
   first TIM14 wrap after that (up to 6.6 s later) reboots the radio. The next boot says
     K6 TXDOG last boot: key-up ran 2xxxx ms, limit 18000 ms: rebooted
   On an image without txdog the radio hangs keyed with the PA off: power-cycle it.
   Upload it without ARQ. Says "dog tx: keyed, PA off, hanging". Not in tests/test_k6_txrules.py
   (it never keys down by design). API level 3. SPDX-License-Identifier: MIT */
#define k6_main cwkey_main
#include "cwkey.c"
#undef k6_main

#define DOG_MS 8000u

int k6_main(const k6_api_t *api) {
  int r = cwkey_main(api);
  if (r <= 0) return r;
  if (api->tx_on(DOG_MS) != 0) return -10;
  api->tx_pa(0);
  api->say("dog tx: keyed, PA off, hanging");
  for (;;) __asm volatile("nop");
}
