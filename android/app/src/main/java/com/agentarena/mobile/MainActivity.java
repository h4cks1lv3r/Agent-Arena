package com.agentarena.mobile;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.net.Uri;
import android.net.http.SslError;
import android.os.Build;
import android.os.Bundle;
import android.text.InputType;
import android.text.TextUtils;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowInsets;
import android.view.inputmethod.InputMethodManager;
import android.webkit.ClientCertRequest;
import android.webkit.CookieManager;
import android.webkit.GeolocationPermissions;
import android.webkit.HttpAuthHandler;
import android.webkit.PermissionRequest;
import android.webkit.RenderProcessGoneDetail;
import android.webkit.ServiceWorkerClient;
import android.webkit.ServiceWorkerController;
import android.webkit.SslErrorHandler;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebStorage;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.webkit.WebViewDatabase;
import android.widget.Button;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import java.io.ByteArrayInputStream;
import java.util.Collections;

/** Remote UI only: no trading loop, broker key storage, or JavaScript bridge. */
public final class MainActivity extends Activity {
    private static final int BG = Color.rgb(11, 20, 33);
    private static final int PANEL = Color.rgb(18, 32, 49);
    private static final int BORDER = Color.rgb(42, 61, 80);
    private static final int TEXT = Color.rgb(237, 244, 250);
    private static final int MUTED = Color.rgb(154, 174, 195);
    private static final int GREEN = Color.rgb(56, 217, 181);
    private static final int ORANGE = Color.rgb(255, 166, 108);
    private LinearLayout root;
    private WebView webView;
    private FrameLayout content;
    private LinearLayout recovery;
    private ProgressBar progress;
    private TextView connectionState;
    private SharedPreferences preferences;
    private volatile OriginPolicy activePolicy;
    private volatile boolean disconnected = true;
    private boolean pageFailed = false;

    @Override public void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        preferences = getSharedPreferences("connection", MODE_PRIVATE);
        WebView.setWebContentsDebuggingEnabled(false);
        getWindow().setStatusBarColor(BG);
        getWindow().setNavigationBarColor(BG);
        root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(BG);
        root.setImportantForAutofill(View.IMPORTANT_FOR_AUTOFILL_NO_EXCLUDE_DESCENDANTS);
        if (Build.VERSION.SDK_INT >= 30) getWindow().setDecorFitsSystemWindows(false);
        root.setOnApplyWindowInsetsListener((view, insets) -> {
            if (Build.VERSION.SDK_INT >= 30) {
                android.graphics.Insets bars = insets.getInsets(WindowInsets.Type.systemBars());
                android.graphics.Insets keyboard = insets.getInsets(WindowInsets.Type.ime());
                view.setPadding(bars.left, bars.top, bars.right, Math.max(bars.bottom, keyboard.bottom));
            } else {
                view.setPadding(insets.getSystemWindowInsetLeft(), insets.getSystemWindowInsetTop(),
                        insets.getSystemWindowInsetRight(), insets.getSystemWindowInsetBottom());
            }
            return insets;
        });
        setContentView(root);
        configureServiceWorkers();
        String savedOrigin = preferences.getString("origin", "");
        if (savedOrigin.isEmpty()) {
            showConnect("");
        } else {
            try { showArena(OriginPolicy.fromInput(savedOrigin)); }
            catch (IllegalArgumentException error) {
                preferences.edit().remove("origin").apply();
                clearSession(() -> showConnect("Enter a valid HTTPS server address."));
            }
        }
    }

    private int dp(float value) { return Math.round(value * getResources().getDisplayMetrics().density); }

    private GradientDrawable background(int color, int stroke, int radius) {
        GradientDrawable drawable = new GradientDrawable();
        drawable.setColor(color);
        drawable.setCornerRadius(dp(radius));
        if (stroke != 0) drawable.setStroke(dp(1), stroke);
        return drawable;
    }

    private TextView text(String value, int sp, int color, boolean bold) {
        TextView view = new TextView(this);
        view.setText(value);
        view.setTextSize(sp);
        view.setTextColor(color);
        view.setTypeface(Typeface.create("sans-serif", bold ? Typeface.BOLD : Typeface.NORMAL));
        view.setLineSpacing(dp(3), 1f);
        return view;
    }

    private void spacer(LinearLayout parent, int height) {
        parent.addView(new View(this), new LinearLayout.LayoutParams(1, dp(height)));
    }

    private Button button(String label, boolean primary) {
        Button button = new Button(this);
        button.setText(label);
        button.setTextSize(15);
        button.setAllCaps(false);
        button.setTextColor(primary ? BG : TEXT);
        button.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        button.setBackground(background(primary ? GREEN : PANEL, primary ? 0 : BORDER, 12));
        button.setMinHeight(dp(50));
        button.setMinimumHeight(dp(50));
        button.setPadding(dp(14), 0, dp(14), 0);
        return button;
    }

    private void showConnect(String message) {
        if (isFinishing() || isDestroyed()) return;
        disconnected = true;
        activePolicy = null;
        root.removeAllViews();
        ScrollView scroll = new ScrollView(this);
        scroll.setFillViewport(true);
        scroll.setClipToPadding(false);
        LinearLayout body = new LinearLayout(this);
        body.setOrientation(LinearLayout.VERTICAL);
        body.setPadding(dp(24), dp(24), dp(24), dp(28));
        scroll.addView(body, new ScrollView.LayoutParams(-1, -2));
        root.addView(scroll, new LinearLayout.LayoutParams(-1, -1));

        TextView brand = text("AGENT  /  ARENA", 16, TEXT, true);
        brand.setLetterSpacing(.16f);
        body.addView(brand);
        spacer(body, 38);
        TextView label = text("PAPER TRADING  ·  REMOTE CLIENT", 11, GREEN, true);
        label.setLetterSpacing(.1f);
        body.addView(label);
        spacer(body, 14);
        body.addView(text("Two minds.\nOne arena.", 38, TEXT, true));
        spacer(body, 14);
        body.addView(text("Follow independent agents, inspect their research, and compare their results side by side.", 16, MUTED, false));
        spacer(body, 26);

        LinearLayout rivals = new LinearLayout(this);
        rivals.setOrientation(LinearLayout.HORIZONTAL);
        rivals.addView(rivalCard("OPENAI", "Independent\nstrategist", GREEN), new LinearLayout.LayoutParams(0, -2, 1));
        View gap = new View(this);
        rivals.addView(gap, new LinearLayout.LayoutParams(dp(12), 1));
        rivals.addView(rivalCard("ANTHROPIC", "Independent\nstrategist", ORANGE), new LinearLayout.LayoutParams(0, -2, 1));
        body.addView(rivals);
        spacer(body, 30);
        body.addView(text("Connect to your server", 21, TEXT, true));
        spacer(body, 8);
        body.addView(text("Use the HTTPS address from your Agent Arena server setup.", 14, MUTED, false));
        spacer(body, 14);
        body.addView(text("SERVER ADDRESS", 11, MUTED, true));
        spacer(body, 8);
        EditText address = new EditText(this);
        address.setSingleLine(true);
        address.setTextColor(TEXT);
        address.setHintTextColor(MUTED);
        address.setTextSize(15);
        address.setHint("https://arena.example.com");
        address.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        address.setBackground(background(PANEL, BORDER, 12));
        address.setPadding(dp(15), dp(13), dp(15), dp(13));
        address.setContentDescription("HTTPS server address");
        address.setText(preferences.getString("origin", ""));
        body.addView(address, new LinearLayout.LayoutParams(-1, dp(54)));
        spacer(body, 10);
        TextView validation = text(message, 13, ORANGE, false);
        validation.setVisibility(message.isEmpty() ? View.GONE : View.VISIBLE);
        body.addView(validation);
        spacer(body, 10);
        Button connect = button("Connect to arena  →", true);
        body.addView(connect, new LinearLayout.LayoutParams(-1, dp(54)));
        connect.setOnClickListener(view -> {
            try {
                OriginPolicy policy = OriginPolicy.fromInput(address.getText().toString());
                connect.setEnabled(false);
                InputMethodManager keyboard = (InputMethodManager) getSystemService(INPUT_METHOD_SERVICE);
                if (keyboard != null) keyboard.hideSoftInputFromWindow(address.getWindowToken(), 0);
                // Store the origin only. Tokens are entered in the server's HTML sign-in form.
                preferences.edit().putString("origin", policy.origin()).apply();
                clearSession(() -> showArena(policy));
            } catch (IllegalArgumentException error) {
                validation.setText(error.getMessage());
                validation.setVisibility(View.VISIBLE);
            }
        });
        spacer(body, 12);
        body.addView(text("Enter your arena access token on the next screen. Broker and AI keys stay on your server.", 13, MUTED, false));
        spacer(body, 28);
        LinearLayout note = new LinearLayout(this);
        note.setOrientation(LinearLayout.VERTICAL);
        note.setPadding(dp(16), dp(15), dp(16), dp(15));
        note.setBackground(background(PANEL, BORDER, 14));
        note.addView(text("The same arena, wherever you are.", 14, TEXT, true));
        spacer(note, 6);
        note.addView(text("While your server is running, the agents can keep working with this app closed. Your phone and web browser show the same server state.", 13, MUTED, false));
        body.addView(note);
        spacer(body, 20);
        body.addView(text("NOT CONNECTED  ·  NO LIVE RESULTS SHOWN", 10, MUTED, true));
        spacer(body, 8);
        body.addView(text("Agent Arena 0.5.0", 11, MUTED, false));
    }

    private LinearLayout rivalCard(String name, String description, int accent) {
        LinearLayout card = new LinearLayout(this);
        card.setOrientation(LinearLayout.VERTICAL);
        card.setPadding(dp(15), dp(17), dp(15), dp(17));
        card.setBackground(background(PANEL, BORDER, 16));
        card.addView(text(name, 12, accent, true));
        spacer(card, 9);
        card.addView(text(description, 14, TEXT, false));
        return card;
    }

    @SuppressLint("SetJavaScriptEnabled")
    private void showArena(OriginPolicy policy) {
        if (isFinishing() || isDestroyed()) return;
        activePolicy = policy;
        disconnected = false;
        pageFailed = false;
        recovery = null;
        root.removeAllViews();
        LinearLayout header = new LinearLayout(this);
        header.setOrientation(LinearLayout.HORIZONTAL);
        header.setGravity(Gravity.CENTER_VERTICAL);
        header.setPadding(dp(14), dp(7), dp(8), dp(7));
        LinearLayout identity = new LinearLayout(this);
        identity.setOrientation(LinearLayout.VERTICAL);
        identity.addView(text("AGENT ARENA", 13, TEXT, true));
        TextView host = text(policy.displayHost(), 11, MUTED, false);
        host.setSingleLine(true);
        host.setEllipsize(TextUtils.TruncateAt.MIDDLE);
        identity.addView(host);
        identity.setContentDescription("Server " + policy.displayHost());
        header.addView(identity, new LinearLayout.LayoutParams(0, -2, 1));
        Button server = toolbarButton("Server");
        server.setOnClickListener(view -> changeServer());
        header.addView(server, new LinearLayout.LayoutParams(dp(63), dp(48)));
        Button reload = toolbarButton("↻");
        reload.setTextSize(26);
        reload.setContentDescription("Reload arena");
        reload.setOnClickListener(view -> reloadArena());
        header.addView(reload, new LinearLayout.LayoutParams(dp(48), dp(48)));
        Button disconnect = toolbarButton("Disconnect");
        disconnect.setTextSize(11);
        disconnect.setContentDescription("Disconnect this phone from the arena");
        disconnect.setOnClickListener(view -> disconnect());
        header.addView(disconnect, new LinearLayout.LayoutParams(dp(80), dp(48)));
        root.addView(header, new LinearLayout.LayoutParams(-1, dp(65)));
        connectionState = text("CONNECTING", 10, GREEN, true);
        connectionState.setPadding(dp(16), 0, dp(16), dp(6));
        root.addView(connectionState);
        progress = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
        progress.setProgressTintList(android.content.res.ColorStateList.valueOf(GREEN));
        progress.setIndeterminateTintList(android.content.res.ColorStateList.valueOf(GREEN));
        root.addView(progress, new LinearLayout.LayoutParams(-1, dp(2)));
        content = new FrameLayout(this);
        root.addView(content, new LinearLayout.LayoutParams(-1, 0, 1));
        webView = new WebView(this);
        webView.setBackgroundColor(BG);
        webView.setImportantForAutofill(View.IMPORTANT_FOR_AUTOFILL_NO_EXCLUDE_DESCENDANTS);
        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true); // Required by this server UI; no native JS bridge exists.
        settings.setDomStorageEnabled(true);
        settings.setDatabaseEnabled(false);
        settings.setAllowFileAccess(false);
        settings.setAllowContentAccess(false);
        settings.setAllowFileAccessFromFileURLs(false);
        settings.setAllowUniversalAccessFromFileURLs(false);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        settings.setJavaScriptCanOpenWindowsAutomatically(false);
        settings.setSupportMultipleWindows(false);
        settings.setGeolocationEnabled(false);
        settings.setSaveFormData(false);
        settings.setMediaPlaybackRequiresUserGesture(true);
        settings.setCacheMode(WebSettings.LOAD_NO_CACHE);
        settings.setUseWideViewPort(true);
        settings.setLoadWithOverviewMode(true);
        settings.setBuiltInZoomControls(false);
        settings.setSafeBrowsingEnabled(true);
        settings.setUserAgentString(settings.getUserAgentString() + " AgentArenaAndroid/0.5.0");
        CookieManager manager = CookieManager.getInstance();
        manager.setAcceptCookie(true);
        manager.setAcceptThirdPartyCookies(webView, false);
        webView.setDownloadListener((url, userAgent, disposition, mime, length) ->
                Toast.makeText(this, "Use your web browser to export arena files.", Toast.LENGTH_LONG).show());
        webView.setWebChromeClient(new WebChromeClient() {
            @Override public void onProgressChanged(WebView view, int value) {
                if (view != webView || disconnected) return;
                progress.setProgress(value);
                progress.setVisibility(value < 100 && !pageFailed ? View.VISIBLE : View.INVISIBLE);
            }
            @Override public void onPermissionRequest(PermissionRequest request) { request.deny(); }
            @Override public void onGeolocationPermissionsShowPrompt(String origin, GeolocationPermissions.Callback callback) {
                callback.invoke(origin, false, false);
            }
            @Override public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> callback, FileChooserParams params) {
                callback.onReceiveValue(null);
                return true;
            }
        });
        webView.setWebViewClient(new WebViewClient() {
            @Override public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                String address = request.getUrl().toString();
                if (allowed(address)) return false;
                if (request.isForMainFrame() && request.hasGesture() && OriginPolicy.isExternalHttps(address)) {
                    confirmExternal(address);
                }
                return true;
            }
            @Override public boolean shouldOverrideUrlLoading(WebView view, String address) {
                // API 26+ uses the request overload. This fallback never opens an external URL.
                return !allowed(address);
            }
            @Override public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest request) {
                return allowed(request.getUrl().toString()) ? null : blockedResponse();
            }
            @Override public void onPageStarted(WebView view, String address, android.graphics.Bitmap icon) {
                if (view != webView || disconnected) return;
                if (!allowed(address)) {
                    view.stopLoading();
                    showRecovery("Navigation blocked", "The page tried to leave your selected HTTPS server. Use Reload to return.");
                    return;
                }
                pageFailed = false;
                if (recovery != null) recovery.setVisibility(View.GONE);
                connectionState.setText("LOADING  ·  " + policy.displayHost());
                connectionState.setTextColor(GREEN);
                progress.setVisibility(View.VISIBLE);
            }
            @Override public void onPageFinished(WebView view, String address) {
                if (view != webView || disconnected || pageFailed || !allowed(address)) return;
                connectionState.setText("HTTPS  ·  " + policy.displayHost());
                progress.setVisibility(View.INVISIBLE);
            }
            @Override public void onReceivedSslError(WebView view, SslErrorHandler handler, SslError error) {
                handler.cancel();
                if (view == webView && !disconnected) {
                    showRecovery("Secure connection failed", "The server certificate could not be verified. Check the server address and certificate, then retry.");
                }
            }
            @Override public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                if (view == webView && request.isForMainFrame() && !disconnected) {
                    showRecovery("Arena is unavailable", "Check your connection and make sure your server is running. Displayed results may be out of date.");
                }
            }
            @Override public void onReceivedHttpError(WebView view, WebResourceRequest request, WebResourceResponse response) {
                if (view == webView && request.isForMainFrame() && response.getStatusCode() >= 500 && !disconnected) {
                    showRecovery("Server needs attention", "Your server returned an error. Retry or check the server before relying on its last displayed state.");
                }
            }
            @Override public void onReceivedHttpAuthRequest(WebView view, HttpAuthHandler handler, String host, String realm) { handler.cancel(); }
            @Override public void onReceivedClientCertRequest(WebView view, ClientCertRequest request) { request.cancel(); }
            @Override public boolean onRenderProcessGone(WebView view, RenderProcessGoneDetail detail) {
                if (view == webView) {
                    destroyWebView();
                    showRecovery("Display restarted", "The Android web renderer stopped. Reconnect to reload your arena.");
                }
                return true;
            }
        });
        content.addView(webView, new FrameLayout.LayoutParams(-1, -1));
        webView.loadUrl(policy.origin() + "/");
    }

    private Button toolbarButton(String label) {
        Button button = new Button(this);
        button.setText(label);
        button.setAllCaps(false);
        button.setTextSize(12);
        button.setTextColor(MUTED);
        button.setBackground(background(BG, 0, 6));
        button.setPadding(0, 0, 0, 0);
        button.setMinWidth(0);
        button.setMinimumWidth(0);
        return button;
    }

    private boolean allowed(String address) {
        OriginPolicy policy = activePolicy;
        return !disconnected && policy != null && policy.allows(address);
    }

    private static WebResourceResponse blockedResponse() {
        return new WebResourceResponse("text/plain", "UTF-8", 403, "Blocked", Collections.emptyMap(),
                new ByteArrayInputStream(new byte[0]));
    }

    private void configureServiceWorkers() {
        ServiceWorkerController controller = ServiceWorkerController.getInstance();
        controller.getServiceWorkerWebSettings().setAllowFileAccess(false);
        controller.getServiceWorkerWebSettings().setAllowContentAccess(false);
        controller.getServiceWorkerWebSettings().setBlockNetworkLoads(true);
        controller.setServiceWorkerClient(new ServiceWorkerClient() {
            @Override public WebResourceResponse shouldInterceptRequest(WebResourceRequest request) {
                return blockedResponse(); // The arena does not need background web workers.
            }
        });
    }

    private void confirmExternal(String address) {
        if (!OriginPolicy.isExternalHttps(address)) return;
        String host = Uri.parse(address).getHost();
        new AlertDialog.Builder(this)
                .setTitle("Open in your browser?")
                .setMessage("This link goes to " + host + ". Your arena stays open here.")
                .setNegativeButton("Cancel", null)
                .setPositiveButton("Open browser", (dialog, which) -> {
                    try {
                        Intent intent = new Intent(Intent.ACTION_VIEW, Uri.parse(address));
                        intent.addCategory(Intent.CATEGORY_BROWSABLE);
                        startActivity(intent);
                    } catch (ActivityNotFoundException error) {
                        Toast.makeText(this, "No browser is available.", Toast.LENGTH_LONG).show();
                    }
                }).show();
    }

    private void showRecovery(String title, String detail) {
        if (isFinishing() || isDestroyed() || content == null) return;
        pageFailed = true;
        if (progress != null) progress.setVisibility(View.INVISIBLE);
        if (connectionState != null) {
            connectionState.setText("NOT CURRENT  ·  CONNECTION NEEDS ATTENTION");
            connectionState.setTextColor(ORANGE);
        }
        if (recovery != null) content.removeView(recovery);
        recovery = new LinearLayout(this);
        recovery.setOrientation(LinearLayout.VERTICAL);
        recovery.setGravity(Gravity.CENTER_VERTICAL);
        recovery.setPadding(dp(28), dp(24), dp(28), dp(24));
        recovery.setBackgroundColor(BG);
        recovery.addView(text("CONNECTION", 11, ORANGE, true));
        spacer(recovery, 14);
        recovery.addView(text(title, 27, TEXT, true));
        spacer(recovery, 12);
        recovery.addView(text(detail, 15, MUTED, false));
        spacer(recovery, 24);
        Button retry = button("Retry connection", true);
        retry.setOnClickListener(view -> reloadArena());
        recovery.addView(retry, new LinearLayout.LayoutParams(-1, dp(52)));
        spacer(recovery, 12);
        Button server = button("Change server", false);
        server.setOnClickListener(view -> changeServer());
        recovery.addView(server, new LinearLayout.LayoutParams(-1, dp(52)));
        spacer(recovery, 18);
        recovery.addView(text("Closing this app does not stop agents running on your server.", 13, MUTED, false));
        content.addView(recovery, new FrameLayout.LayoutParams(-1, -1));
    }

    private void reloadArena() {
        OriginPolicy policy = activePolicy;
        if (policy == null || disconnected) { showConnect(""); return; }
        if (webView == null) { clearSession(() -> showArena(policy)); return; }
        pageFailed = false;
        if (recovery != null) recovery.setVisibility(View.GONE);
        webView.stopLoading();
        webView.loadUrl(policy.origin() + "/");
    }

    private void changeServer() {
        clearSession(() -> showConnect("Session cleared. Enter a server address to reconnect."));
    }

    private void disconnect() {
        preferences.edit().remove("origin").apply();
        clearSession(() -> showConnect("Phone disconnected. Agents on your server are not stopped."));
    }

    private void destroyWebView() {
        WebView old = webView;
        webView = null;
        if (old == null) return;
        old.stopLoading();
        old.setWebChromeClient(null);
        old.setWebViewClient(new WebViewClient());
        old.clearHistory();
        old.clearCache(true);
        old.clearFormData();
        if (old.getParent() instanceof ViewGroup) ((ViewGroup) old.getParent()).removeView(old);
        old.removeAllViews();
        old.destroy();
    }

    private void clearSession(Runnable after) {
        disconnected = true;
        activePolicy = null;
        destroyWebView();
        WebStorage.getInstance().deleteAllData();
        WebViewDatabase.getInstance(this).clearHttpAuthUsernamePassword();
        CookieManager manager = CookieManager.getInstance();
        manager.removeAllCookies(removed -> {
            manager.flush();
            if (!isFinishing() && !isDestroyed()) after.run();
        });
    }

    @Override public void onBackPressed() {
        if (webView != null && webView.canGoBack() && !disconnected) {
            webView.goBack();
        } else {
            // Back is navigation, not Disconnect. Android may finish this
            // activity or move its task to the background. The saved origin
            // and browser session must survive either outcome.
            super.onBackPressed();
        }
    }

    @Override protected void onPause() {
        if (webView != null) webView.onPause();
        CookieManager.getInstance().flush();
        super.onPause();
    }

    @Override protected void onResume() {
        super.onResume();
        if (webView != null) webView.onResume();
    }

    @Override protected void onDestroy() {
        disconnected = true;
        activePolicy = null;
        destroyWebView();
        super.onDestroy();
    }
}
