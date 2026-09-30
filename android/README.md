# Agent Arena Android client — 0.6.1

This is a native Java Android shell for the same responsive interface served by your Agent Arena server. It does not run trading agents or contain broker/model credentials. The server must remain running and have an HTTPS address reachable by the phone. No server is supplied or silently deployed by installing the APK.

## Use on your phone

1. Install `Agent_Arena_v0.6.1.apk` on Android 8.0 or newer. Permit installation from the specific app you use to open the downloaded APK if Android requests it.
2. Enter your server origin, for example `https://arena.example.com`. Include an HTTPS port if required. Do not add a path, token, username, or password to this address.
3. Enter the arena access token in the server's sign-in page. The server issues the browser session cookie; the Android app never reads the token or stores it in preferences.
4. The app loads the same arena as your web browser. Use the native Reload button to reconnect or Server to change the address.

The app remembers only the server origin in its preferences. Android's WebView stores the server-issued cookie under standard browser rules until it expires. A normal app restart reconnects to the saved origin. Back moves to the previous page when one exists. At the dashboard root, Back uses Android's normal app navigation and retains the saved origin and browser session. Server change and Disconnect clear cookies, web storage, cache, and HTTP authentication data. Disconnect also forgets the saved origin. **Disconnecting or closing the phone app does not halt agents running on your server.** Use the arena's server controls to stop agents.

## Connection boundaries

- HTTPS only with system certificate authorities; certificate errors are cancelled.
- Strict scheme, hostname, and port checks. The app does not accept token-bearing connection URLs.
- Same-origin WebView navigation and intercepted resources. HTTPS links to other sites require a deliberate browser confirmation. Unsupported URL schemes are blocked.
- No JavaScript-to-native bridge, file access, content access, mixed content, file picker, downloads, extra windows, geolocation, microphone, or camera access.
- Third-party cookies and background service-worker network traffic are disabled. Server security headers provide the additional page-level content restrictions.
- Internet is the only requested permission. Android backups and WebView debugging are disabled.
- No local synthetic trading display is used. The disconnected screen explicitly shows no live results.

Use your trusted Arena server. WebView's resource interception callback does not cover every subsequent subresource redirect, so the client is not a complete network firewall against a malicious server. Main-page redirects are also checked by the navigation guard; the Arena server's same-origin Content Security Policy protects its normal page resources.

## Build and update

The app uses the Android framework without third-party libraries or Gradle. Install Java 17+ including `jdk.compiler` and `keytool`, and the official Android SDK Platform 35 and Build Tools 35.0.0.

```sh
python3 android/build.py \
  --android-jar /path/to/android-sdk/platforms/android-35/android.jar \
  --build-tools /path/to/android-sdk/build-tools/35.0.0 \
  --signing-dir /private/path/to/agent-arena-signing \
  --output /path/to/Agent_Arena.apk
```

The signing directory must be outside the Android source tree. A new directory creates a release identity once. Future builds reuse it. Restore both the PKCS12 keystore and password file from the separate private signing backup before building an update. Never put the private signing backup in a public repository or in the APK/source archive.

The build runs 33 JVM checks for origin isolation and 25 regression checks for the production Back handler, creates the APK, checks alignment, and verifies its signatures. The Back tests compile the shipped handler into an Activity/WebView harness; they check navigation and session-clearing calls, not Android lifecycle dispatch or cookie storage on a phone. Build evidence is under `android/build/`. This does not replace an installation and connection test on a real Android phone.

Application ID: `com.agentarena.mobile`. Version code: `601`. Minimum API: `26`. Target API: `35`.

The v0.6.1 APK uses the existing release signing certificate and installs over our v0.5.1 APK. It displays the updated server's stock and USD crypto dashboard. The server runs the research and trading logic; install the matching server release before restarting the paper test.

## Official implementation references

- [Android SDK tools](https://developer.android.com/tools)
- [Unsafe URI loading in WebViews](https://developer.android.com/privacy-and-security/risks/unsafe-uri-loading)
- [WebSettings](https://developer.android.com/reference/android/webkit/WebSettings)
- [WebViewClient and TLS callbacks](https://developer.android.com/reference/android/webkit/WebViewClient)
- [APK signing tool](https://developer.android.com/tools/apksigner)
- [Android tasks and the Back stack](https://developer.android.com/guide/components/activities/tasks-and-back-stack)

Only public documentation and SDK packages were fetched during development. No production broker or paid model calls are required to build this client.
