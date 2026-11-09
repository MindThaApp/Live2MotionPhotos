# Microsoft Store submission checklist

Product: **iPhone Live Photo to Android Motion Photo Converter Portal** (Store ID `9P8MMJZMTGSF`)
Partner Center product type: **MSIX or PWA app**. Package: MSIX, packaged Win32 desktop app
(full trust), x64, Windows 10 1809+ / Windows 11.

## 1. Partner Center account and name reservation (done)

- [x] Developer account (individual or company) at https://partner.microsoft.com/dashboard
- [x] Name reserved: **iPhone Live Photo to Android Motion Photo Converter Portal**
- [ ] Optional: reserve a shorter extra name (e.g. "Live2Motion Photos") under
      Product management > Manage product names, in case certification objects to the
      trademarks in the current name (see step 11).

## 2. Identity values (done; this is the one config spot)

Partner Center > the app > Product management > **Product identity** shows these values.
They are already in `packaging/windows/store-identity.json`:

| Partner Center field | store-identity.json key | Value |
|---|---|---|
| Package/Identity/Name | `IdentityName` | `34458MindtheApp.iPhoneLivePhototoAndroidMotionPhot` |
| Package/Identity/Publisher | `Publisher` | `CN=FE63C3BB-418B-484C-852F-E7985F260BC3` |
| Package/Properties/PublisherDisplayName | `PublisherDisplayName` | `MindtheApp` |
| Reserved app name | `DisplayName` | `iPhone Live Photo to Android Motion Photo Converter Portal` |
| Package Family Name (check) | `ExpectedPackageFamilyName` | `34458MindtheApp.iPhoneLivePhototoAndroidMotionPhot_24e29cj9741rt` |

- The Identity Name is cut short by Partner Center ("...MotionPhot"). Keep it exactly
  like that.
- `DisplayName` must be one of the names reserved for this app, or the upload is
  rejected. Partner Center checks the manifest's `Properties/DisplayName`. The build uses
  the full reserved name (58 characters) for both `Properties/DisplayName` and the Start
  menu name (`VisualElements/DisplayName`); both allow up to 256 characters, so nothing is
  shortened. Tiles show the short name "Live2Motion" (`ShortName`, limit 40).
- `Version` (now `1.1.0.0`) must have 0 as its last part and must go up for every new
  submission, for example `1.1.1.0` or `1.2.0.0`.
- The family-name suffix `24e29cj9741rt` is derived from the Publisher value. It has been
  checked against the Publisher above, and the CI sideload test checks it again on the
  installed package.

## 3. Build the package

Pick one:

- **CI:** push the branch, then run *Actions > Microsoft Store MSIX > Run workflow*, or push
  a tag `store-v1.1.0`. Download the `msix-store-upload` artifact.
- **Local:** in PowerShell on Windows with Python 3.12 and the Windows 10/11 SDK:
  ```powershell
  py -3.12 -m venv venv; .\venv\Scripts\python -m pip install -r requirements-desktop.txt
  .\packaging\windows\build.ps1 -OutDir C:\somewhere\store-build -Python .\venv\Scripts\python.exe
  ```
  The packages are written to `<OutDir>\run-<timestamp>\packages\`.

Files:
- `..._1.1.0.0_x64.msixupload`: **upload this to Partner Center** (the plain `.msix` works too).
  It is unsigned on purpose, because the Store signs it.
- `..._x64_sideload.msix` + `sideload-test-cert.cer`: test build for your own PC only (step 4).

## 4. Sideload test before submitting (recommended)

Signed with a self-signed test certificate whose subject is the Publisher above.

1. Double-click `sideload-test-cert.cer` > Install Certificate > **Local Machine** > Place
   all certificates in **Trusted People**. Admin rights are needed.
2. Double-click `..._sideload.msix` > Install.
3. Start "iPhone Live Photo to Android Motion Photo Converter Portal" from Start. Test it on a **copy** of
   a few Live Photos, never on your only originals.
4. Afterwards, uninstall the app from Settings > Apps. You can remove the test certificate
   in `certlm.msc` > Trusted People.

About the test certificate: `build.ps1` keeps the sideload certificate's private key as
`cert\sideload-test.pfx` in the build output folder, with its password in plain text next to
it (`sideload-test.pfx.password.txt`). That is acceptable **only because it is a throwaway
test certificate** for sideloading on your own PC: it is not the Store signing identity (the
Store signs the upload itself) and must never be used to sign anything you distribute. Keep
the build output folder out of git (the default `store-build-out/` is already ignored; if you
pass `-OutDir` inside the repository, do not commit its `cert\` folder) and do not share it.

## 5. Pricing and availability

- Markets: all, or choose.
- Visibility: Public (or Private / hidden for a first test submission).
- Pricing: **Free** (the source is MIT-licensed on GitHub). No free trial is needed.
- Release: "as soon as it passes certification", or manual publish.

## 6. Properties

- Category: **Photo & video**. Secondary category: Utilities & tools (optional).
- Privacy policy URL: see step 7. A URL is required, because the package declares a
  restricted capability (runFullTrust).
- Website: https://github.com/MindThaApp/Live2MotionPhotos
- Support contact: https://github.com/MindThaApp/Live2MotionPhotos/issues
- System requirements: Minimum Windows 10 version 1809, x64. "Recommended" can stay empty.
- Product declarations: tick none of the account/payment ones. "This app depends on
  non-Microsoft drivers or NT services": No. Leave "Customers can install this app to
  alternate drives or removable storage" ticked.

## 7. Privacy policy URL

The text is in `store/PRIVACY.md`. Options:

- **Simplest:** after this branch is merged to `main` and pushed, use
  `https://github.com/MindThaApp/Live2MotionPhotos/blob/main/store/PRIVACY.md`
- **Nicer:** GitHub Pages. In repo Settings > Pages, choose *Deploy from a branch* > `main` >
  `/ (root)`. The policy is then at
  `https://mindthaapp.github.io/Live2MotionPhotos/store/PRIVACY` (GitHub Pages renders
  Markdown files as pages).

Partner Center question "Does this product access, collect or transmit personal
information?": **No**. The app reads only the photo folders the user picks, keeps
everything on the PC and makes no internet connections.

## 8. Age ratings (IARC questionnaire)

- Category: **Utility, productivity, communication or security app** (or "Other app").
- Answer **No** to violence, sexual content, gambling, drugs, language and similar
  questions.
- Users interact or share content with other users: **No**. Shares location: **No**.
  Digital purchases: **No**. Unrestricted internet access (web browser): **No**.
- Expected result: 3+ / Everyone (PEGI 3, ESRB Everyone).

## 9. Packages

Upload the `.msixupload`. Device family: **Windows 10/11 Desktop**. Partner Center should
show architecture x64, min version 10.0.17763.0, and the restricted capability
`runFullTrust`.

### runFullTrust justification

Paste this into "Submission options" > "Restricted capabilities" (it is requested
automatically when the package declares `runFullTrust`):

> This is a packaged Win32 desktop app (Python, packaged as MSIX). runFullTrust is required
> for any desktop app packaged this way. The app converts photo/video pairs in folders the
> user selects: it reads and writes ordinary files in those folders, starts its bundled
> converter process and the bundled ExifTool, and hosts its interface in Microsoft Edge
> WebView2 through a local server bound to 127.0.0.1. It makes no internet connections,
> installs no services or drivers, and changes no system settings.

## 10. Store listing (English)

Use `store/listing.md`: description, what's new, features, keywords, copyright line.

- **Screenshots (required):** at least 1, up to 10. PNG, **1920x1080 recommended** (anything
  from 1366x768 up to 3840x2160 is accepted). Suggested set, made on sample photos only:
  1. the main screen with folders chosen;
  2. a Preview with the results table;
  3. a finished run with the progress donut;
  4. the Extract tab.
  To capture: start the app, press Win+Shift+S (or Alt+PrtScn for the window), then save
  as PNG. Use a 1920x1080 display, or resize the window to about 1920x1080.
- **Store logos:** the build generates `assets/store/`. The CI artifact
  `store-listing-images` contains `box-art-1080x1080.png` / `box-art-2160x2160.png` (1:1 box
  art), `poster-art-720x1080.png` (2:3 poster art) and `tile-icon-300x300.png`. Upload at
  least the 2:3 poster and the 1:1 box art; Windows 10/11 Store pages use them.
- Trailers and the other image types are optional.

## 11. Notes for certification

Paste into "Notes for certification":

> No account or sign-in is needed, and the app works offline. To test: create a folder with
> an iPhone Live Photo pair (for example IMG_0001.HEIC + IMG_0001.MOV, or JPG + MOV), choose
> it as the input folder and an empty folder as the output folder, click Preview, then
> Convert. The output folder receives a single Motion Photo per pair, and the input files
> are not changed. The interface is shown in Microsoft Edge WebView2 and is served from a
> local server bound to 127.0.0.1 with a per-session access key; it does not open any
> internet connections. Requires the WebView2 Runtime (part of Windows 11 and current
> Windows 10).

Things certification may flag:
- **Trademarks in the name** ("iPhone", "Android"). Store policy 10.1.4 restricts other
  companies' trademarks in names, but descriptive use is common and the listing carries a
  non-affiliation notice. If it is rejected, reserve a neutral name (for example
  "Live2Motion Photos: Live Photo to Motion Photo"), put it in
  `store-identity.json` > `DisplayName`, rebuild and resubmit. The identity Name/Publisher
  stay the same.
- **WebView2 Runtime** is preinstalled on Windows 11 and on Windows 10 with current updates.
  If it cannot be started, the app is built to show a message with Microsoft's download
  link instead of crashing. This path has not been tested on a PC without WebView2.
- **Windows App Certification Kit:** optionally run it locally before submitting (Start >
  "Windows App Cert Kit" > Validate Store App, choose the sideload `.msix`). It needs admin
  rights.
- The copyright line names the MIT licence holder (Ramin Azizi). Change it to "MindtheApp"
  if you prefer.

## 12. Submit, then later updates

1. Submit for certification. It usually takes 1 to 3 business days.
2. For an update: raise `Version` in `store-identity.json` (e.g. `1.1.1.0`), build, start a
   new submission, upload the new `.msixupload` and update "What's new".
