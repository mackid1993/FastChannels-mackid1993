# CLAUDE.md

Everything learned building and verifying this repo, for future Claude sessions (and people). `AGENTS.md` covers the repo layout, the CI lifecycle and the rules for resolving a patch conflict; read it too.

Use American spelling everywhere.

## What this repo is

A patch overlay on [kineticman/FastChannels](https://github.com/kineticman/FastChannels) `main` (his release branch, so every build is released code). CI applies `patches/` to upstream, validates, builds, smoke-tests and boot-tests the image, and publishes `ghcr.io/mackid1993/fastchannels-mackid1993:latest` (public; pulls need no login). Weekly full build (Mondays 9 AM Eastern), daily drift check, AI conflict resolution with Aider on OpenRouter (`OPENROUTER_API_KEY` secret; the model is set by the `AI_MODEL` variable and the workflow default, currently GLM 5.3, `openrouter/z-ai/glm-5.3`). The patch refreshes its own context after every successful run, so upstream drift rarely turns into a conflict.

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

## User-Agent: the app's real string (and why not `Custom-Exoplayer`)

Before 2026-10-01 the relay sent python-requests' UA on the bridge master fetch (the request that opens the Yospace session) and a Windows Chrome UA on playlists and segments. On 2026-10-01 both were briefly switched to `Custom-Exoplayer`, the string hard-coded in DirecTV's Android TV `ExoPlayerWrapper`. Side by side on CNN (same account, same URL, only the UA differed), the old UAs got inserted ads at 23:53 and 23:55 and the `Custom-Exoplayer` session got none. Reverted. That string is only the app's non-Cronet fallback; with Cronet (`CRONET_LEVEL = 2`) the app's player sends the native `AnalyticsService::generateUserAgent()` string: `APP_PROJECT_NAME/<app version> (<OS name> <OS version>; <Build.MODEL>; <Build.BOARD>)  PureRN/0.79.5` (literal `APP_PROJECT_NAME`, two spaces before `PureRN`). Any UA change must be A/B tested for inserted ads first.

The patch now sends that real app string (A/B tested 2026-10-02: inserted ads in the same breaks as the old UAs, 8 each), built per bridge device from `getprop ro.build.version.release`, `ro.product.model` and `ro.product.board` (a Fire TV Stick 4K Max: `APP_PROJECT_NAME/5.0.136.2002113867 (Android 11; AFTKRT; karat)  PureRN/0.79.5`). Sources: `AnalyticsService::generateUserAgent()` (literals `APP_PROJECT_NAME`, `/`, ` (`, ` `, `; `, `) `, ` PureRN/`, then 0.79.5), `DeviceInformation::getDeviceOSName()` = `Android`, `getAppVersion()` = `JManifestInfoProvider::getVersionNumber()` = the package versionName, and `Java_com_clientapp_cronetservice_CronetHttpService_getUserAgent` = `AnalyticsService::getInstance()->getUserAgent()`, which Cronet sends because `CustomCronetDataSourceFactory` never sets its own UA.

## Opt-in: stereo downmix (louder AC-3 audio) — `app/scrapers/dtv_stereo_downmix.py`

The "Downmix DirecTV audio to stereo" toggle (`stereo_downmix`, **off by default**, its own module) makes the proxied master declare DirecTV's AC-3 audio rendition `CHANNELS="2"` instead of `"6"`. On Android 11 the stick's media3 sets the AC-3 decoder's channel count from the master's CHANNELS attribute: with `"6"` the platform decoder does its own band-limited 5.1→2.0 downmix (muffled, and a few dB quieter after the fold-down), while `"2"` makes it emit AC-3's own full-range Lo/Ro stereo downmix — fuller and a few dB **louder**. The point now is loudness: it lifts the quiet AC-3 programming (~−34 LUFS) toward the hot inserted-ad level (the AAC ads measure ~−20 LUFS, ~5 dB over their AC-3 twins), so a bridge stick with a stereo-only (PCM) encoder can then raise the whole feed uniformly on the LinkPi/receiver without the ads blasting. Only the master's AC-3 `#EXT-X-MEDIA` CHANNELS attribute changes — the 384 kbps AC-3 segments, the codec, the STREAM-INF lines and the other renditions are untouched; media playlists pass through unchanged. Leave it off for an output that passes AC-3 through to an AV receiver (keep 5.1).

Hooks (one line each): `*dtv_stereo_downmix.CONFIG_FIELDS` in directv.py's `config_schema`, and `dtv_stereo_downmix.stereo_downmix_master(r.text, channel.source.config)` wrapping the master in `directv_browser_manifest`. History: this is the old `keep_surround` stereo-downmix (added, then reverted 2026-10-02 because it didn't fix the muffle), brought back on 2026-10-02 as an opt-in **loudness** lever in its own file. The pre-DAI "Surround sound toggle" was a separate, unrelated setting removed 2026-10-01.

## Android TV sign-in (`app/scrapers/dtv_android.py`)

Added 2026-10-02. The DirecTV source authenticates end to end as DirecTV's own Android TV app (OAuth client `UNIFIED_Android_TV_02`) via its device-code grant, replacing kineticman's web-browser sign-in (`UNIFIED_DTV_WEB`). So the bearer, the DRM activation/license and the Yospace ad session are all Android TV instead of a web browser wearing an Android-TV hat on one request. `directv.py` gets one-line hooks.

`dtv_android.py` is **portable** — it imports nothing from `directv_dai`, so the whole Android-TV client (the device-code grant/refresh, the bridge-device reading, the app User-Agent, and the channel/v2 request) can be adopted without the DAI feature. The dependency points one way: `directv_dai` (DAI) imports `dtv_android`, never the reverse. The DAI ad-targeting context a login needs (DMA/consent, profile ids via `login_fields`) is applied by `directv_dai.store_login_result`, not inside `dtv_android`.

**No stored credentials.** There is no username/password field. The user clicks Authenticate, approves the device code in a browser at directv.com/tvsigninv2, and that's it; the source is "configured" once it has a captured session (`is_source_config_complete('directv')` = has a bearer or refresh token, like Philo/ESPN). The admin UI has an Authenticate/Re-authenticate button and a **Log out** button (`POST /api/sources/<id>/directv-logout` clears the session keys + the cached Yospace playback). `directv_auto_login` requires no creds.

The grant (all endpoints and shapes verified live + against the web HAR; the opaque tokens mean nothing is a readable JWT):
- **Start:** `POST api.cld.dtvce.com/account/device/grant/v2/devicecode` with JSON `{clientId, deviceClassID}` (no `creationFlow` needed) → `{deviceCode, userCode, displayURL, url, pollingInterval, …}`. No `registrationToken` despite what a first read of the bundle suggested.
- **Approve (manual, one-time):** the user opens the `url` (`directv.com/tvsigninv2/<CODE>/UNIFIED_Android_TV_02`, code embedded) in a browser signed in to their account and approves — the grant's approve step is the tvsigninv2 **web page's** own call (WAF-protected, account-session-cookie auth), not an API we can drive, so it is genuinely the user's one manual step. `capture_android_auth` surfaces the code + link in the admin status and polls. `usercode/validate` (GET) only *checks* a code; it does not grant.
- **Poll:** `GET …/account/device/grant/tokens?clientID=<id>&deviceCode=<dc>` (deviceCode alone, unauthenticated) → `202 pending` until approved, then `access_token` + `refresh_token` + `valuePairs`.
- **Refresh:** `POST …/authn-refreshgo/v3/refresh?clientID=<id>` with form `clientMake`, `clientModel`, `refresh_token` (snake_case; clientID in the query). Rotates the refresh token. `refresh_session()` keeps the saved `dtv_android_device_id`, so the account keeps one registered device.
- **DRM/license + channel/v2** are already identical to the app; only the headers change — `license_request_headers` returns `dtv_android.license_headers` (the app UA, no `stream.directv.com` Origin/Referer).

**Refresh policy — never relog, no timer, don't fail a tune.** The grant token is long-lived, so `token_stale()` does **not** refresh on a clock (it's stale only when there's no bearer, or — if the response ever carries a real expiry, which it currently doesn't — just before it). The token rides until a request actually rejects it. At tune time, if `resolve()`'s channel authorization comes back expired, `dtv_android.refresh_in_place(self)` refreshes inline and retries once so the tune still succeeds (persisted via `_update_config`); it only fires on a real expiry — never on a cache hit or a channel flip. If that inline refresh can't recover (no/dead refresh token), `_mark_auth_stale_and_reauth` just clears the cached playback and logs that the user must re-authenticate — there are no stored credentials to auto-reauth with, and a background refresh with the same dead token wouldn't help. (A DRM license/activation failure is the separate `_directv_trigger_reauth` path — see "DRM re-auth" below: it delegates to `dtv_android.drm_reauth`, which drops only the cached DRM identity so the next tune re-activates with the stored `activation_token`, never wipes that token, and queues no background refresh.) `sign_in(source_id, …)` is the run_directv_auth entry: refresh an existing Android TV session, else run the one-time device-code grant. There is no web/password sign-in.

**Reverted (2026-10-02): the `CHANNELS="2"` stereo-downmix / `keep_surround`.** It declared DirecTV's AC-3 rendition as 2-channel in the proxied master to dodge the bridge's band-limited in-decoder 5.1→2.0 downmix. Tested live: it only made the AC-3 downmix *louder*, never restored the high end — the muffle is in DirecTV's per-ad AC-3 encode (see the 7 kHz findings below), not the channel count. Removed so we ship only what works.

## DRM re-auth: never wipe the activation token (`dtv_android.drm_reauth`)

Two credentials back DirecTV DRM: the **activation token** — the durable root; it comes only from the device-code **grant** (it's in the grant's `valuePairs`, absent from the refresh's, confirmed live) and dies only on a password reset/revoke — and the short-lived **identity cookie** minted from it (cached in redis `directv_identity:<sid>`, re-minted per device session). Upstream's `_directv_trigger_reauth` (called on a DRM 403 — activation 1001 or license 1009) was built for the web client: it popped `activation_token` + `identity_cookie` and queued a background refresh. For an Android TV session that's fatal — a refresh never returns a new activation token, so popping it strands DRM in a 1001/1009 loop that also storms DirecTV. The one-line hook `if dtv_android.drm_reauth(source, reason): return` takes that case over: `drm_reauth` flushes only the cached identity cookie (redis + config), **keeps** the activation token (and bearer/refresh), and queues **no** refresh, so the next tune re-activates cleanly with the stored token. When the activation token is genuinely gone it logs that re-auth is needed and stops (no storm); `dtv_android.sign_in` then runs a fresh grant (not a futile refresh) to restore it. Returns False for a non-Android-TV session so upstream's old web recovery still runs. Verified live 2026-10-03: a simulated license 1009 dropped the cached cookie, kept the token, no storm, and the next tune re-activated and played.

## channel/v2 (the Android TV app's endpoint)

The Android TV app's bundled config authorizes channels on `right/authorization/channel/v2` (the web uses v1). Same query params; the response has `playbackData.streamUrls: [{groupName: 'DAI'|'Data Center', URLs: [...]}]` and the same `dRights.playToken`. Measured 2026-10-02 on CNN: v2's DAI URLs had the same Yospace host, path and parameter names as v1's `streamURL` (one identical after masking tokens). With DAI on the patch uses v2 to match the app (merged into `dtv_android.android_auth_request()`, which returns the v2 URL). The app adds `hevc`/`maxHevcLevel`/`maxAvcLevel`/`hdr`/`deviceModelIdentifier` only when its remote `dualEncodeEnabled` flag is on; not sent.

## Android TV authorization request

With DAI on, `dtv_android.android_auth_request()` (part of the portable Android-TV client — moved out of `directv_dai`) drops the web player's `Origin`/`Referer`, sends the device's app User-Agent, and sends the app's query: `ccid`, `proximity`, `clientContext`, `reserveCTicket=true`, `daiEnabled=true`, `startOver=false`, `abrEnabled=true` (from `getQueryParams` in the app bundle; the web-only `timeShiftEnabled` and `dualManifest` are dropped). Tested 2026-10-02: 200, authorized, play token present, same Yospace host and identical DAI URL parameter set as the web request.

## Stable device ids

Every session uses the device's saved values from `/data/directv_dai_devices.json` (keyed by adb address): one stable identity per device, even when it's asleep or adb is down. New devices are read over adb immediately and saved. Known devices are re-checked in the background about hourly; only a real change (a factory reset's new Android ID, an ad-tracking toggle, firmware) updates the saved values, logged as `[directv-dai] device <addr> changed (...)`. Failed re-checks are ignored. Nothing to do by hand. History: before 2026-10-02 a failed read cached `{}` for an hour, sending sessions with no device id (account consent, mostly national spots).

## Hooks

See `AGENTS.md` for the full hook table: every one-line hook in upstream's files, what it does, and where to put it back if upstream moves the code. The Android TV hooks in `directv.py` are `dtv_android.sign_in` (in `run_directv_auth`), `dtv_android.license_headers` (DRM headers), `dtv_android.token_stale` (`_token_stale`), `dtv_android.android_auth_request` (the channel/v2 request, in `_fetch_channel_playback`), `resolve()`'s inline `dtv_android.refresh_in_place` + retry, and `_directv_trigger_reauth`'s `dtv_android.drm_reauth` delegation (DRM-failure recovery); the relay (`directv_proxy.py`) uses `dtv_android.player_headers()`, `dtv_aac_ads.swap_muffled_ads()` (per media playlist), `dtv_aac_gain.attenuate_ad_segment()` (per inserted-ad AAC segment) and `dtv_stereo_downmix.stereo_downmix_master()` (per master). Everything else lives in `directv_dai.py`, `dtv_android.py`, `dtv_aac_ads.py`, `dtv_aac_gain.py` and `dtv_stereo_downmix.py`.

## Removed: one DRM session through inserted ads (keep_drm_session)

`keep_drm_session()` dropped Yospace's `#EXT-X-KEY:METHOD=NONE` so FC Player kept its secure decoders through ads (like the Android TV app's `setUseDrmSessionsForClearContent`). Tried, briefly re-added, and **reverted for good on 2026-10-02 because it reduced ad targeting** — dropping the `METHOD=NONE` marker changed the session enough that breaks came back with less-targeted (national) fill. It never changed the ad audio anyway (that's in DirecTV's per-creative AC-3 files; see the 7 kHz findings). The audio fix is the AAC swap below.

## Inserted-ad audio: the AAC swap (`app/scrapers/dtv_aac_ads.py`)

Many inserted-ad creatives are band-limited (~7 kHz) in their AC-3 rendition while the *same* creative's HE-AAC twin is full range — upstream and the Osprey play the same AC-3 files, so no flag, header or identity changes which file is served. `swap_muffled_ads()` (its own module, hooked in `directv_browser_asset` next to the relay) rewrites each inserted ad on the DAI AC-3 audio playlist — every segment **and** the ad's own `#EXT-X-MAP` init, matched by the Yospace ad-creative name `u-<id>-c-<rate>-…mp4` on the ad CDN — to the `-a-96-` HE-AAC twin. Yospace emits the ad's MAP itself, so it's a pure URI rewrite; live `dfwlive-*` content never matches (channel audio untouched), and it's a safe no-op on any other playlist. The clear ads keep `METHOD=NONE`. Trade-offs: swapped ads play **stereo** (the AAC twin is 2.0), and *every* inserted AC-3 ad is swapped (every measured AAC twin was full range, so there's no per-ad muffle detection in the request path). Consistent with the fairness rules — same creative, DirecTV's own rendition, delivered and counted the same. **Unverified by ear as of 2026-10-02:** needs a live inserted ad through a build with this change; if a swapped ad comes out *silent* rather than muffled, FC Player didn't accept the AAC twin's init.

## Inserted-ad loudness: the −4.5 dB AAC gain cut (`app/scrapers/dtv_aac_gain.py`)

The AAC-swapped ads play ~4.5 dB hotter than the programming (measured 2026-10-03: with the stereo-downmix on, content sits at −24.5 LUFS — the US broadcast standard — while the inserted AAC ads are ~−20). `attenuate_ad_segment()` (its own always-on module, hooked in `directv_browser_asset` as the swap's byte-level sibling) pulls each inserted AAC ad down to match — losslessly, no re-encode: it edits each AAC-LC frame's `global_gain` down 3 steps (1.5 dB each = −4.5 dB). **No toggle; implicitly DAI-gated** — it only matches the `u-<id>-a-96-<n>-<n>.mp4` twins the swap creates, never live `dfwlive-*` content, and no-ops on the `-i` init. It's an original implementation of the ISO/IEC 14496-3 AAC-LC bitstream (the Huffman + scalefactor-band tables are the standard's data — no third-party code), walking SCE/CPE `raw_data_block`s including the full channel-1 spectral skip needed to reach a stereo CPE's channel-2 `global_gain`, parsing the fMP4 `moof/trun` for per-frame sizes, and auto-detecting the sample rate. **Safety:** it edits only if every frame parses cleanly to `END` at one rate; on any mismatch it returns the bytes unchanged, so it can never emit a corrupted segment. ffmpeg self-test (dev-only): stereo, mono and 44.1 kHz all drop exactly −4.5 dB, same byte length, clean decode; init and non-AAC bytes untouched. Consistent with the fairness rules — DirecTV's own creative, delivered and counted the same, just at the right level.

## The DAI patch in one paragraph

The opt-in "Use DirecTV ad insertion (DAI)" toggle makes DirecTV playback use `streamURL` (a Yospace server-side ad-insertion session) instead of `fallbackStreamUrl`, adds `yospace.pool=livepause` (Yospace answers 503 without it), and adds the ad flags DirecTV's own Android TV app sends. The authorization request (`channel/v2`, app query, no browser headers) and the relay's User-Agent match the Android TV app too. The ad-insertion logic lives in `app/scrapers/directv_dai.py`, the Android-TV client in the portable `app/scrapers/dtv_android.py` (DAI depends on it, never the reverse), the inserted-ad AAC swap in `app/scrapers/dtv_aac_ads.py`, the inserted-ad loudness cut in `app/scrapers/dtv_aac_gain.py`, and the opt-in stereo-downmix in `app/scrapers/dtv_stereo_downmix.py`; upstream files get the one-line hooks in the table in `AGENTS.md`.

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

- **Signing in as the Android TV app doesn't change the ad session.** Tested 2026-10-01 with DirecTV's device-code sign-in (`account/device/grant/v2/devicecode`, `clientId=UNIFIED_Android_TV_02`, approved at directv.com/tvsigninv2): CNN's `streamURL` came back identical to the web sign-in's (same host, `aegdfwprd01` profile and parameters). App client IDs: web `UNIFIED_DTV_WEB`, Android TV `UNIFIED_Android_TV_02`, Fire TV `UNIFIED_Android_TV`, Osprey/DirecTV boxes `UNIFIED_AEPS`. (As of 2026-10-02 we nonetheless sign in as Android TV end to end — see "Android TV sign-in" above — for an honest, consistent client identity and a grant token that refreshes without a web re-login, **not** to change the ad streamURL, which this confirms it doesn't.)
- **`is_lat=0` with a device id is what gets local and political ads** (live A/B, 2026-09-28, CNN prime time). With `is_lat=1` (the account's web opt-out), breaks got national spots and 1:30–2:00 of "Commercial Break In Progress" slate. With `is_lat=0` and the device's own id, the same account got neighborhood ads (a local martial-arts school, Bob's Discount Furniture, Volvo Cars Ramsey, NY political ads) and short slates. `android_id:` did this on both a Fire TV and a Google TV, so no advertising id is needed.
- **The Google advertising ID can't be read over adb** (Play services keeps it; root-only). Fire OS exposes its own advertising id and `limit_ad_tracking` as secure settings (no root needed), but the user wants every device treated alike, so no device sends an advertising id: all send `android_id:<Android ID>`. `limit_ad_tracking=1` still switches a Fire TV to the opt-out form.
- **The 7 kHz ad audio is in DirecTV's ad files,** whatever the flags. National ads are part of the channel's live encode and are full range; inserted ads come from DirecTV's per-creative transcode, and many of their AC-3 renditions are band-limited.
- **DAI doesn't change picture quality.** Blockiness and blur measured the same across DAI configurations on the same content.
- **Don't forward the stick's raw `Dalvik/...` User-Agent to Yospace.** Tried 2026-09-28 (the bridge's server-side master fetch sent the Fire TV's `Dalvik/... AFTKRT` UA instead of python-requests'). Different from the app UA the patch sends now (see above). It did not fix the 7 kHz AC-3 ads (the encode is fixed per ad file, the same for every viewer), and the next break served wrong-market local ads (a St. Louis Kia dealer for a DMA 501 account). Reverted.
- **Settled 2026-10-02: the Osprey plays the same 7 kHz AC-3 ad files.** On CNN the Osprey opens a Yospace Pause Live session (Yospace SDK 3.11.2, `d=osprey`, `yo.cps=b.lp.d.y.224-3630.0x.s.n`, `yo.po=32`, `yo.aal=true`, same `cps.7950` profile). Its logcat (`VSTB load started for URL`) showed it loading `yospace01-directv.akamaized.net/dtv-prd/<id>/02001/u-6600-c-384-1-N.mp4` for two inserted ads; every segment decoded to 7.0–7.1 kHz in both ffmpeg and Apple's AudioToolbox Dolby decoder, and the user confirmed they sounded muffled on the Osprey too. The same ad's AAC rendition (`u-6600-a-96`) decodes full range. Nothing in the request (identity, User-Agent, channel/v1 vs v2, HEVC fields) changes which ad file is served. (The `CHANNELS="2"` stereo-downmix tried to dodge this on the bridge's decode side — reverted 2026-10-02, see "Android TV sign-in" above — because live it only changed loudness, not the high end, confirming the muffle is this per-ad AC-3 encode in DirecTV's files, not the stick's downmix.)
- **The 7 kHz ads are in DirecTV's ad files.** Each inserted ad has one AC-3 and one AAC encode at a fixed CDN path; many ads' AC-3 encode is band-limited to ~7 kHz while their AAC is full range. No request flag or header changes which file is served. On the stick, an inserted ad shows as a switch from the secure to the non-secure video decoder (EventLogger `videoDecoderInitialized`). To check an ad: its segments on the ad CDN are unencrypted; decode the AC-3 `u-6600-c-384-1-N.mp4` (raw AC-3 frames start at `mdat`) with ffmpeg or `afconvert` and measure.
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
  - With the stereo-downmix toggle on, that content rises to **−24.5 LUFS** (the US broadcast standard; measured 2026-10-03 on the stick's LinkPi feed), while the inserted AAC ads land ~**−20** — so `dtv_aac_gain` cuts them −4.5 dB to match (see "Inserted-ad loudness"). Measure a stick's output on its encoder feed (`http://<linkpi>:8090/stream<N>`, mapped per stick in ah4c `settings.json`) with `ffmpeg … -af ebur128`.
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
