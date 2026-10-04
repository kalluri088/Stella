# Stella Browser

Stella can fetch a page as *static* text (`web_fetch`, `network_read`), but that
never runs JavaScript — so a page whose content is drawn by scripts comes back
almost empty. The browser capability adds the one thing a fetcher cannot do:
drive a **real, already-installed browser** in headless mode, let the page's
scripts run, and hand back either the rendered text or a picture of it. Read
this before switching it on; a full browser is the largest attack surface Stella
can touch.

## What it is

Two tools, one family, both gated by a single opt-in:

- **`browser_read`** — load one https URL, let it render, return the visible DOM
  text (scripts executed), bounded and wrapped in the same
  `<<<UNTRUSTED_WEB_CONTENT>>>` markers as every other external read.
- **`browser_screenshot`** — load one https URL and write one PNG of the rendered
  page into the Stella workspace, size-bounded, and report the path back. Stella
  does **not** look at the pixels; the picture is a file for you to open.

It adds **no dependency and downloads nothing**: it only runs a browser already
on the machine — a Chromium-family binary probed on `PATH`, or pointed at
explicitly with `STELLA_BROWSER` (a path). With no browser found the tools answer
a structured *"browser is off"* instead of pretending, exactly as the web tools
do when no backend is present.

## The fences

1. **On in the desktop app; off for headless unless you turn it on.**
   `browser_tools_enabled` defaults to **on** for the saved/desktop configuration,
   so the *Browser* checkbox in Settings starts ticked; the bare and
   environment (`STELLA_MODEL`, `stella voice`) paths keep it **off** until you
   opt in. While off the model never sees the capability — it is not registered.
   Turn it back off with the *Browser* checkbox (saved) or for one launch with
   `STELLA_BROWSER_TOOLS=off`; turn a headless run on with
   `STELLA_BROWSER_TOOLS=on` (the environment wins over the saved box in both
   directions). Note this is deliberately a **different** variable from
   `STELLA_BROWSER`, which names the browser *binary*: a path like
   `/usr/bin/chromium` must never be mistaken for "turn the capability on".
2. **Every single use asks.** The floor is `RiskLevel.DANGEROUS`, so the
   dispatcher stops for a trusted approval before any page loads. You see the
   **literal address**, the browser it will open in, and an honest note about the
   jail state. The model cannot pre-approve or answer for you.
3. **Only a vetted public https URL is opened.** The same scheme, length,
   no-credentials and public-address checks `web_fetch` runs first (localhost and
   `.local`/`.internal` refused; a dotted-quad literal must be a global address),
   and at execute time the hostname is resolved and **rejected if any answer is
   private, loopback, link-local or otherwise not global**.
4. **Private addresses are name-blocked inside the browser too.** Chromium is
   started with `--host-resolver-rules` mapping loopback, RFC1918, link-local,
   CGNAT, `.local`/`.internal` and the cloud-metadata address to `~NOTFOUND`, so
   a redirect or a sub-resource a page pulls in cannot quietly reach them — this
   closes the DNS-rebinding hole fence 3 (a one-time resolve) cannot.
5. **A throwaway profile, never yours.** The browser runs with a fresh
   `--user-data-dir` under a private temp directory and an isolated `HOME`, so it
   touches none of your real browser profile, cookies, logins or history. The
   scratch is deleted when the render finishes.
6. **The filesystem jail when bubblewrap is present.** The same read-only /
   home-hidden bubblewrap jail that guards `shell_run` (see `docs/SHELL_TOOLS.md`
   and `stella.sandbox`) also wraps the browser process, so even a renderer
   exploit sees the host read-only. When bubblewrap is unavailable the browser
   runs under **its own** Chromium sandbox — `--no-sandbox` is passed *only*
   inside the jail, never outside it — and the result says the jail was not
   active. `STELLA_SHELL_SANDBOX=0` turns the jail off (and this with it); the
   browser then relies on fences 3–5 plus Chromium's own sandbox.
7. **Bounded and honestly reported.** A wall-clock render budget plus a hard
   subprocess timeout takes the whole browser process group down (on the
   `stella.childproc` parent-death guarantee, so a stuck render cannot outlive
   Stella); DOM text is read under a byte cap; a screenshot is size-capped; stdin
   is closed. Rendered text is treated exactly like fetched web content — wrapped
   and defanged against marker forgery.

## What "jail" still does not mean

This shrinks the blast radius of loading a hostile page; it is **not** a promise
against a Chromium zero-day. The page still loads over the network, and a full
browser is a big program. That is exactly why the capability is offered only in
the desktop app by default (off for the `STELLA_MODEL` and `stella voice` paths)
and asks on every use, and why your approval of the literal address is the
authority — the fences only decide the damage *given* a load happens. For hard
isolation, run Stella itself in a container or VM.

## What is intentionally *not* done

- **No interactive automation.** This is one-shot headless render, not a driving
  session. There is no click / type / wait-for-selector / navigate-back, and no
  persistent browser daemon between calls. Building that would mean a new hard
  dependency (Playwright/Selenium) and a fragile stateful process — both against
  the project's "small, understandable system" rule. If a page needs a form
  filled, that is a different capability that has not been asked for.
- **No reading of screenshot pixels.** `browser_screenshot` saves the PNG for the
  human; Stella never analyzes the image, so there is no vision model wired in.

## How to validate it

The unit tests (`tests/test_browser_tools.py`) prove every decision through
injected seams — no browser, no network — including the URL/DNS refusals, the
three jail states and their warnings, the `--no-sandbox`-only-in-jail and bwrap
routing, the host-resolver rules, the throwaway profile, timeout/truncation/
over-cap shaping, untrusted wrapping and marker-forgery defang, the "browser is
off" result, and the config round-trip with the environment override in both
directions. Where a Chromium is installed, one bounded local-file render runs
through the real renderer to prove the byte cap and clean process teardown. A
live render of a real https page needs the network and your approval; run it from
the app, not a unit test, so the behavior is observed rather than asserted.
