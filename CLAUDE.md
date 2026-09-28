# CLAUDE.md

Everything learned building and verifying this repo, for future Claude sessions (and people). `AGENTS.md` covers the repo layout, the CI lifecycle and the rules for resolving a patch conflict; read it too.

Use American spelling everywhere.

## What this repo is

A patch overlay on [kineticman/FastChannels](https://github.com/kineticman/FastChannels) `development`. CI applies `patches/` to upstream, validates, builds, smoke-tests and boot-tests the image, and publishes `ghcr.io/mackid1993/fastchannels-mackid1993:latest` (public; pulls need no login). Weekly full build (Mondays 9 AM Eastern), daily drift check, AI conflict resolution with Aider on OpenRouter (`OPENROUTER_API_KEY` secret, `AI_MODEL` variable). The patch refreshes its own context after every successful run, so upstream drift rarely turns into a conflict.

Upstream declined the DAI feature (PR #65: no measurable quality/CDN difference, and he doesn't think it changes account risk), and the relay-bypass PR #66. That is why this overlay exists.

## The DAI patch in one paragraph

The opt-in "Use DirecTV ad insertion (DAI)" toggle makes DirecTV playback use `streamURL` (a Yospace server-side ad-insertion session) instead of `fallbackStreamUrl`, adds `yospace.pool=livepause` (Yospace answers 503 without it), and adds the ad flags DirecTV's own Android TV app sends. Nearly all of it lives in `app/scrapers/directv_dai.py`; upstream files get about 15 one-line hooks.

## The flags, and where each value comes from

Every value comes from DirecTV's own clients. None is guessed.

| Group | Flags | Source |
|---|---|---|
| Device identity | `d=android_tv`, `nielsen_platform=plt,OTT`, `nielsen_dev_group=devgrp,STV`, `metr=1071`, `comscore_platform=android`, `comscore_impl_type=a` | Android TV app (`com.att.tv` leanback APK): its shared player sets these for Android TV |
| App constants | `p=dfw`, `e=prod`, `m=live`, `attnid=dfw003`, `_fw_nielsen_app_id=P7CFE36D…`, `at=NOW.RR`, `ut=bbtv` | Identical in the web capture, the Osprey config and the Android TV bundle |
| Yospace flags | `yospace.pool=livepause`, `yo.po=32`, `yo.lpa=true`, `yo.lp=true`, `yo.fr=true`, `yo.av=5` | Web capture plus the Android TV app's defaults. Yospace itself writes the rest (`yo.asd`, `yo.cps`, `yo.ec`, …) into the playlist |
| Account values | `hhid`/`u` (partnerProfileId), `profid`, `dma_location`/`dma_billing`, `gpp`, `gpp_sid`, `is_lat` | The signed-in account, fetched at login. `is_lat` is decoded from GPP only when `gpp_sid` is 7 |
| Device IDs | `adid`, `_fw_did`, `comscore_device` | Random, minted once per install |
| Channel | `net` | `daiChannelName` from DirecTV's lineup |
| Omitted on purpose | `us_privacy` when null (the Android TV app omits it), `bZipCode` (only sent when DirecTV's `cgnatEnabled` flag is on, which it currently isn't), `ltlg`, `dvadid`, `yo.vm` (Osprey app-bundle macro) | — |

### Hard-won facts

- **`d` is what the ad server keys on.** Same account, same breaks, run side by side:
  - no `d`: **0** inserted ads;
  - `d=android_tv`, `firetv`, `osprey` or `desktop`: ads inserted;
  - `androidtv`, `aft`, `atv`: 0 (not recognized).
- **Never send the desktop identity** (`d=desktop`, `plt,DSK`, `devgrp,DSK`, comScore `PC`/`b`, `metr=47`). It pulls **web** ad inventory: wrong-market spots (the user saw Virginia ads) and badly encoded ones. 3 of 9 inserted ad files in one capture were band-limited to 7 kHz at the source (they sound like AM radio).
- With `d=android_tv` the user gets local NYC ads (DMA 501) with normal audio.
- `smoke_test.py` asserts `d=android_tv`, the Android TV Nielsen/comScore values, `metr=1071`, no `us_privacy`, and no desktop flags.

### How the values were found (reproducible)

- **Web client:** HAR captures of stream.directv.com. The desktop request has 37–38 parameters. Its JS bundle (`att-purern.*.bundle.js`) contains DirecTV's shared player code with a per-platform ad-flag table (`paramOptions`: `DEVGRP_*`, `ADID_PREFIX_*`) and `metrParam {tv:1071, phone:47, tablet:47}`.
- **Osprey** (`com.att.tv.openvideo`, native Android):
  - `assets/config_servers/prod/AndroidNGC_OSPREY/AppConfiguration.json` → `payload.clientConfig.dai` (`deviceList[{device_type:osprey, dai_device_type:osprey, nielsen_dev_group:devgrp,STV}]`, comScore `OTT`/`a`, `metr 1071`, `attnid`, …);
  - the request builder is `com.att.ott.common.playback.player.quickplay.vstb.YospaceRequestParamGenerator` (decompile with `jadx`).
- **Android TV** (`com.att.tv`, React Native, Hermes bytecode in `assets/index.android.bundle`):
  - decompile with `pip install hermes-dec` → `hbc-decompiler index.android.bundle dec.js` (~80 MB);
  - `yospaceParameters` sets `d='android_tv'` for non-handset, non-tablet Android;
  - `universalYospaceParameters` sets `comscore_platform = systemName.toLowerCase()`, omits `us_privacy` when it's `'null'`, and adds `bZipCode` only when `cgnatEnabled` is on.
- **DirecTV's runtime config** (`api.cld.dtvce.com/ux/client/config/v3/client/config2?version=&deviceName=Phoenix&env=prod&applicationId=1`) needs a signed-in client, and returns config for the app the token was issued to. **Fetching the Android TV version would mean signing in as that app. Don't.** This is the one unverifiable gap: DirecTV may push extra Yospace tuning flags at runtime.

## Delivery and quality findings (measured)

- **The video bytes are identical** for DAI and non-DAI, whether direct or through the FastChannels relay, and on every CDN host (Fastly `-ms`, Akamai `-os`, CloudFront `-sponsored…cf.dtvcdn.com`, Cloudflare `-os.live.cflare`). SHA-256 matched every time.
- **CDN speed is about the same:** 31–35 Mbps and ~190–235 ms time to first byte from the LAN. DAI streams usually ride the `sponsored` route. The top 1080p rung needs 6.5 Mbps.
- **The main difference is the path:**
  - with DAI, the stick fetches straight from the CDN, because Yospace hosts aren't on the relay allowlist;
  - without DAI, the bridge hands the stick `browser.m3u8`, and every playlist and segment goes through FastChannels' `browser-asset` relay.
  - The user saw non-DAI-through-the-relay as softer; direct non-DAI looked as good as DAI. This isn't proven numerically.
- **What DAI really provides:** local ads in the slots that are otherwise filler ("interdimensional cable"), the normal DirecTV client path, and an honest identity. Not better pixels.
- **Fire TV bridge picture:** set Color Format to **YCbCr, 8-bit**. The wrong setting lifts blacks and exposes blocking.
- **Audio:**
  - DirecTV's audio runs about **-34 LUFS**, roughly 10 dB below normal TV. It's the AC-3 5.1 track, decoded by Fire OS and downmixed.
  - The LinkPi `gain` setting at +6 dB sounded distorted to the user, so it's back at 0.
  - Muffled/"AM radio" moments were bad ad files (see above), channel audio (MS Now had a dull stretch on both tracks and every box), or a stuck player app, which a reboot fixed.
- **When comparing boxes,** each DAI session gets its **own** ads and a different delay behind live. Compare only non-ad content, or align frames by content, and swap boxes to rule out hardware.

## Operating notes

- **Clear the DirecTV stream cache** after any flag change (cached Yospace URLs live 55 minutes):
  ```
  docker exec fastchannels sqlite3 /data/fastchannels.db "UPDATE source_cache SET value='{}' WHERE cache_key='directv_playback' AND source_id=(SELECT id FROM sources WHERE name='directv');"
  ```
  Toggling DAI in the UI also clears it.
- **Check what a server sends:** `curl -s -D - -o /dev/null http://<server>:5523/play/directv/<ccid>.m3u8 | grep -i location` and parse the query. Look at keys and constant values only; the URL carries session tokens.
- **Check a stick's quality level:** `adb shell dumpsys SurfaceFlinger | grep -A1 "SurfaceView - com.fastchannels"` shows the decode size.
- **Count inserted ads:** poll a DAI session's AC-3 audio playlist. Segments not on a `dfwlive-*` host are inserted ads, served unencrypted from `yospace01-directv.akamaized.net`.
- **Never pull or log bearer or play tokens** into chats or logs. Field names only.
- **`:latest` updates on the Monday build.** Rollback is a pin to `:upstream-<sha>` or `:build-<key>`.
