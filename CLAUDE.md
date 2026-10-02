# CLAUDE.md

Everything learned building and verifying this repo, for future Claude sessions (and people). `AGENTS.md` covers the repo layout, the CI lifecycle and the rules for resolving a patch conflict; read it too.

Use American spelling everywhere.

## What this repo is

A patch overlay on [kineticman/FastChannels](https://github.com/kineticman/FastChannels) `main` (his release branch, so every build is released code). CI applies `patches/` to upstream, validates, builds, smoke-tests and boot-tests the image, and publishes `ghcr.io/mackid1993/fastchannels-mackid1993:latest` (public; pulls need no login). Weekly full build (Mondays 9 AM Eastern), daily drift check, AI conflict resolution with Aider on OpenRouter (`OPENROUTER_API_KEY` secret; model pinned to DeepSeek V4.1 Flash, `openrouter/deepseek/deepseek-v4.1-flash`, in the `AI_MODEL` variable and the workflow default). The patch refreshes its own context after every successful run, so upstream drift rarely turns into a conflict.

Upstream declined the DAI feature (PR #65: no measurable quality/CDN difference, and he doesn't think it changes account risk), and the relay-bypass PR #66. That is why this overlay exists.

## Why this exists: fairness, not features

The DAI patch exists so this setup treats DirecTV and its advertisers fairly, not to get a better picture or to dodge anything. A paying subscriber should watch the way DirecTV intends, with each party getting its fair share:

- **DirecTV and the networks** get their ad inventory filled and counted through their own ad-insertion system, instead of local ad slots sitting empty or showing filler.
- **Advertisers** reach the market they paid for (the account's real DMA), with the ads delivered and counted.
- **The subscriber's choices are respected.** `is_lat` follows the playback device's own ad-tracking setting, the way DirecTV's TV apps do it (the Osprey reads it from the device, not the account's web consent). A device that limits ad tracking sends the apps' opted-out form. When the requester isn't a known bridge device, the account's GPP consent decides.
- **We tell the truth about ourselves.** Every value is either the account's own data or DirecTV's own published client value for what the playback device really is (Android TV). Nothing is invented, nothing pretends to be a device we aren't, and nothing is logged that shouldn't be.

When changing this code, keep that goal ahead of convenience:
- If you don't know the correct value, leave it out rather than guess.
- Don't impersonate another client or device beyond the Android TV identity DirecTV's own app uses.
- Don't sign in as another app.
- Don't send fabricated ad-view or measurement data.
- Don't do anything to skip or suppress ads.

## User-Agent: `Custom-Exoplayer` kills ad insertion (reverted)

The relay sends python-requests' UA on the bridge master fetch (the request that opens the Yospace session) and a Windows Chrome UA on playlists and segments. On 2026-10-01 both were switched to `Custom-Exoplayer`, the string hard-coded in DirecTV's Android TV `ExoPlayerWrapper`. Side by side on CNN (same account, same URL, only the UA differed), the old UAs got inserted ads at 23:53 and 23:55 and the `Custom-Exoplayer` session got none. Reverted. That string is only the app's non-Cronet fallback; with Cronet (`CRONET_LEVEL = 2`) the app's player sends the native `AnalyticsService::generateUserAgent()` string: `APP_PROJECT_NAME/<app version> (<OS name> <OS version>; <Build.MODEL>; <Build.BOARD>)  PureRN/0.79.5` (literal `APP_PROJECT_NAME`, two spaces before `PureRN`). Any UA change must be A/B tested for inserted ads first.

The patch now sends that real app string, built per bridge device from `getprop ro.build.version.release`, `ro.product.model` and `ro.product.board` (a Fire TV Stick 4K Max: `APP_PROJECT_NAME/5.0.136.2002113867 (Android 11; AFTKRT; karat)  PureRN/0.79.5`). Sources: `AnalyticsService::generateUserAgent()` (literals `APP_PROJECT_NAME`, `/`, ` (`, ` `, `; `, `) `, ` PureRN/`, then 0.79.5), `DeviceInformation::getDeviceOSName()` = `Android`, `getAppVersion()` = `JManifestInfoProvider::getVersionNumber()` = the package versionName, and `Java_com_clientapp_cronetservice_CronetHttpService_getUserAgent` = `AnalyticsService::getInstance()->getUserAgent()`, which Cronet sends because `CustomCronetDataSourceFactory` never sets its own UA.

The Surround sound toggle was removed 2026-10-01 at the user's request (dead weight); bridge sticks always get DirecTV's full master, AC-3 included.

## channel/v2 (the Android TV app's endpoint)

The Android TV app's bundled config authorizes channels on `right/authorization/channel/v2` (the web uses v1). Same query params; the response has `playbackData.streamUrls: [{groupName: 'DAI'|'Data Center', URLs: [...]}]` and the same `dRights.playToken`. Measured 2026-10-02 on CNN: v2's DAI URLs had the same Yospace host, path and parameter names as v1's `streamURL` (one identical after masking tokens). With DAI on the patch now uses v2 anyway, at the user's request, to match the app. The app adds `hevc`/`maxHevcLevel`/`maxAvcLevel`/`hdr`/`deviceModelIdentifier` only when its remote `dualEncodeEnabled` flag is on; not sent.

## Android TV authorization request

With DAI on, `android_auth_request()` drops the web player's `Origin`/`Referer`, sends the device's app User-Agent, and sends the app's query: `ccid`, `proximity`, `clientContext`, `reserveCTicket=true`, `daiEnabled=true`, `startOver=false`, `abrEnabled=true` (from `getQueryParams` in the app bundle; the web-only `timeShiftEnabled` and `dualManifest` are dropped). Tested 2026-10-02: 200, authorized, play token present, same Yospace host and identical DAI URL parameter set as the web request.

## Hooks

See `AGENTS.md` for the full hook table: every one-line hook in upstream's files, what it does, and where to put it back if upstream moves the code. 18 hooks as of 2026-10-02 (the DAI flag and channel-name hooks were collapsed: `_fetch_channel_playback` takes one `dai` flag dict, and `note_channel(self, row, ccid)` writes the cache itself).

## Removed: one DRM session through inserted ads

`keep_drm_session()` dropped Yospace's `#EXT-X-KEY:METHOD=NONE` so FC Player kept its secure decoders through ads (like the Android TV app's `setUseDrmSessionsForClearContent`). It worked (no decoder switch) but didn't change the ad audio, which is in DirecTV's files, so it was removed 2026-10-02 to keep only what targeting needs.

## The DAI patch in one paragraph

The opt-in "Use DirecTV ad insertion (DAI)" toggle makes DirecTV playback use `streamURL` (a Yospace server-side ad-insertion session) instead of `fallbackStreamUrl`, adds `yospace.pool=livepause` (Yospace answers 503 without it), and adds the ad flags DirecTV's own Android TV app sends. Nearly all of it lives in `app/scrapers/directv_dai.py`; upstream files get about 15 one-line hooks.

## The flags, and where each value comes from

Every value comes from DirecTV's own clients. None is guessed.

| Group | Flags | Source |
|---|---|---|
| Device identity | `d=android_tv`, `nielsen_platform=plt,OTT`, `nielsen_dev_group=devgrp,STV`, `metr=1071`, `comscore_platform=android`, `comscore_impl_type=a` | Android TV app (`com.att.tv` leanback APK): its shared player sets these for Android TV |
| App constants | `p=dfw`, `e=prod`, `m=live`, `attnid=dfw003`, `_fw_nielsen_app_id=P7CFE36D…`, `at=NOW.RR`, `ut=bbtv` | Identical in the web capture, the Osprey config and the Android TV bundle |
| Yospace flags | `yospace.pool=livepause`, `yo.lpa=true`, `yo.lp=true`, `yo.fr=true`, `yo.av=5`, `yo.sl=3`, `yo.d.cp=true`, `yo.cps=b.lp.d.s.180-3630.0x.s.n`, `yo.vm=<base64>` | The Android TV app's full live set (its bundled `YSLiveParams` plus the `atv` overrides). `yo.lpa`/`yo.lp` are required (no ads without them). `yo.vm` is its ad-macro map (APPBUNDLE `com.att.tv`). The web player's `yo.po=32` is not sent |
| Account values | `hhid`/`u` (partnerProfileId), `profid`, `dma_location`/`dma_billing`, `gpp`, `gpp_sid`, `bZipCode` | The signed-in account, fetched at login. `bZipCode` is the billing ZIP from the location service (`billingDmas[].zipcode`), fetched lazily if login didn't store `dai_zip` |
| Device ad id + consent | `_fw_did=android_id:<Android ID>`, `is_lat=0`; limited tracking: `google_advertising_id:optout`, `adid=optout`, `is_lat=1`; all: `comscore_device=Android_<manufacturer>_<model>` | Read over adb from the requesting bridge device (one call, cached an hour), same logic on every device (`device_ad_flags`). `android_id:` is DirecTV's own prefix. The Android TV app itself sends the platform advertising id (on Fire TV it reads `Settings.Secure advertising_id` and sends it as `google_advertising_id:<id>` + `adid`; elsewhere the Play-services id). A Google TV/Shield only exposes that through Play services (not adb), and the user wants every device treated alike (2026-10-02), so no device sends its advertising id. `comscore_device` is the app's `universalYospaceParameters` value `${getSystemName()}_${getDeviceManufacturer()}_${getDeviceModel()}`, whitespace removed. The app's own null-id fallback is `android_id:` + `getDeviceId()` (= `Build.BOARD`), never reached in practice |
| Channel | `net` | `daiChannelName` from DirecTV's lineup |
| Omitted on purpose | `us_privacy` when null (the Android TV app omits it), `ltlg` (the app sends lat/long, 2 decimals, only when its location service has a fix; unverified whether a Fire TV ever does, so not sent), randomly minted ids, `dvadid`, `yo.aal`/`yo.po` (Osprey/web only) | — |

### Hard-won facts

- **Signing in as the Android TV app doesn't change the ad session.** Tested 2026-10-01 with DirecTV's device-code sign-in (`account/device/grant/v2/devicecode`, `clientId=UNIFIED_Android_TV_02`, approved at directv.com/tvsigninv2): CNN's `streamURL` came back identical to the web sign-in's (same host, `aegdfwprd01` profile and parameters). App client IDs: web `UNIFIED_DTV_WEB`, Android TV `UNIFIED_Android_TV_02`, Fire TV `UNIFIED_Android_TV`, Osprey/DirecTV boxes `UNIFIED_AEPS`.
- **`is_lat=0` with a device id is what gets local and political ads** (live A/B, 2026-09-28, CNN prime time). With `is_lat=1` (the account's web opt-out), breaks got national spots and 1:30–2:00 of "Commercial Break In Progress" slate. With `is_lat=0` and the device's own id, the same account got neighborhood ads (a local martial-arts school, Bob's Discount Furniture, Volvo Cars Ramsey, NY political ads) and short slates. `android_id:` did this on both a Fire TV and a Google TV, so the Fire TV advertising id isn't needed.
- **The Google advertising ID can't be read over adb** (Play services keeps it; root-only). Fire OS exposes its advertising id and `limit_ad_tracking` as secure settings, but the Android ID fallback works everywhere, so the patch uses it for all devices.
- **The 7 kHz ad audio is in DirecTV's ad files,** whatever the flags. National ads are part of the channel's live encode and are full range; inserted ads come from DirecTV's per-creative transcode, and many of their AC-3 renditions are band-limited.
- **DAI doesn't change picture quality.** Blockiness and blur measured the same across DAI configurations on the same content.
- **Don't forward the stick's User-Agent to Yospace.** Tried 2026-09-28 (the bridge's server-side master fetch sent the Fire TV's `Dalvik/... AFTKRT` UA instead of python-requests'). It did not fix the 7 kHz AC-3 ads (the encode is fixed per ad file, the same for every viewer), and the next break served wrong-market local ads (a St. Louis Kia dealer for a DMA 501 account). Reverted.
- **Settled 2026-10-02: the Osprey plays the same 7 kHz AC-3 ad files.** On CNN the Osprey opens a Yospace Pause Live session (Yospace SDK 3.11.2, `d=osprey`, `yo.cps=b.lp.d.y.224-3630.0x.s.n`, `yo.po=32`, `yo.aal=true`, same `cps.7950` profile). Its logcat (`VSTB load started for URL`) showed it loading `yospace01-directv.akamaized.net/dtv-prd/<id>/02001/u-6600-c-384-1-N.mp4` for two inserted ads; every segment decoded to 7.0–7.1 kHz in both ffmpeg and Apple's AudioToolbox Dolby decoder, and the user confirmed they sounded muffled on the Osprey too. The same ad's AAC rendition (`u-6600-a-96`) decodes full range. Nothing in the request (identity, User-Agent, channel/v1 vs v2, HEVC fields) changes which ad file is served.
- **The 7 kHz ads are in DirecTV's ad files.** Each inserted ad has one AC-3 and one AAC encode at a fixed CDN path; many ads' AC-3 encode is band-limited to ~7 kHz while their AAC is full range. No request flag or header changes which file is served. On the stick, an inserted ad shows as a switch from the secure to the non-secure video decoder (EventLogger `videoDecoderInitialized`).
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
- **Since 2026-09-28 DAI takes the relay path too** (`yospace.com` is on the relay allowlist). Before that, DAI bypassed the relay, which was the only code difference between DAI and non-DAI delivery.
- **The main difference was the path:**
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
