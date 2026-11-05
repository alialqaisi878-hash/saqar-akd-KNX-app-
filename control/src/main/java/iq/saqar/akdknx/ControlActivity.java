package iq.saqar.akdknx;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.net.Uri;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.Gravity;
import android.view.View;
import android.view.WindowManager;
import android.webkit.CookieManager;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.CheckBox;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;

import java.net.HttpURLConnection;
import java.net.URL;

/**
 * TAT SMART: the control app for the home owner (phone) or the wall screen.
 * It only opens the house page: the company link from anywhere, or the bridge on this device.
 */
public class ControlActivity extends Activity {
    private static final String BASE = "https://knx.saqar-akd.com.iq/h/";
    private static final String LOCAL = "http://127.0.0.1:8080/";
    private static final int PICK_FILE = 7;
    private SharedPreferences prefs;
    private WebView web;
    private ValueCallback<Uri[]> pending;
    private final Handler ui = new Handler(Looper.getMainLooper());

    @Override protected void onCreate(Bundle b) {
        super.onCreate(b);
        prefs = getSharedPreferences("akd", MODE_PRIVATE);
        if (prefs.getString("url", "").isEmpty()) showSetup(); else showPanel();
        Updater.check((code, name, err) -> {
            if (err != null || code <= Updater.localCode(this)) return;
            runOnUiThread(() -> new android.app.AlertDialog.Builder(this)
                    .setTitle("نسخة جديدة من TAT SMART")
                    .setMessage("النسخة " + name + " جاهزة. تريد تحدّث هسه؟")
                    .setPositiveButton("تحديث", (d, w) -> {
                        Toast.makeText(this, "جاري التحميل...", Toast.LENGTH_SHORT).show();
                        Updater.install(this, (c2, n2, e2) -> {
                            if (e2 != null) runOnUiThread(() -> Toast.makeText(this, "ما كدر يحمّل: " + e2, Toast.LENGTH_LONG).show());
                        });
                    })
                    .setNegativeButton("بعدين", null)
                    .show());
        });
    }

    private void showSetup() {
        LinearLayout l = new LinearLayout(this);
        l.setOrientation(LinearLayout.VERTICAL);
        l.setLayoutDirection(View.LAYOUT_DIRECTION_RTL);
        l.setGravity(Gravity.CENTER_VERTICAL);
        l.setPadding(56, 56, 56, 56);
        l.setBackgroundColor(Color.parseColor("#0a101c"));

        ImageView logo = new ImageView(this);
        logo.setImageResource(R.drawable.ic_tat);
        l.addView(logo, new LinearLayout.LayoutParams(-1, 260));
        l.addView(text("TAT SMART", 30, "#3fd0b0"));
        l.addView(text("اكتب اسم البيت اللي انطاك ياه الفني", 15, "#94a3b8"));
        EditText house = new EditText(this);
        house.setHint("مثلاً ahmed-villa");
        house.setHintTextColor(Color.parseColor("#64748b"));
        house.setTextColor(Color.WHITE);
        house.setSingleLine(true);
        house.setTextDirection(View.TEXT_DIRECTION_LTR);
        house.setText(prefs.getString("house", ""));
        l.addView(house);

        CheckBox local = new CheckBox(this);
        local.setText("هذا الجهاز هو تابليت البيت (عليه KNX Prog)");
        local.setTextColor(Color.parseColor("#e2e8f0"));
        local.setChecked(prefs.getBoolean("local", false));
        l.addView(local);

        Button go = new Button(this);
        go.setText("دخول");
        go.setOnClickListener(v -> {
            String h = house.getText().toString().trim().toLowerCase();
            boolean isLocal = local.isChecked();
            if (!isLocal && !h.matches("[a-z0-9_-]{3,40}") && !h.startsWith("http")) {
                Toast.makeText(this, "اسم البيت: حروف إنگليزية وأرقام و - فقط", Toast.LENGTH_LONG).show();
                return;
            }
            String url = isLocal ? LOCAL : h.startsWith("http") ? (h.endsWith("/") ? h : h + "/") : BASE + h + "/";
            prefs.edit().putString("house", h).putBoolean("local", isLocal).putString("url", url).apply();
            showPanel();
        });
        l.addView(go);
        l.addView(text("الباسورد يطلبه مرة وحدة بعد الدخول.", 13, "#64748b"));
        setContentView(l);
    }

    @SuppressLint("SetJavaScriptEnabled")
    private void showPanel() {
        final String url = prefs.getString("url", "");
        boolean isLocal = prefs.getBoolean("local", false);
        if (isLocal) getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);

        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(Color.parseColor("#0a101c"));
        web = new WebView(this);
        web.getSettings().setJavaScriptEnabled(true);
        web.getSettings().setDomStorageEnabled(true);
        // tells the page it runs inside TAT SMART: customer controls only, no technician login
        web.getSettings().setUserAgentString(web.getSettings().getUserAgentString() + " TATSMART");
        CookieManager.getInstance().setAcceptCookie(true);
        CookieManager.getInstance().setAcceptThirdPartyCookies(web, true);
        web.setWebViewClient(new WebViewClient() {
            @Override public void onReceivedError(WebView v, WebResourceRequest r, WebResourceError e) {
                if (!r.isForMainFrame()) return;
                v.loadData("<html><body style='background:#0a101c;color:#e2e8f0;font-family:sans-serif;display:flex;"
                        + "align-items:center;justify-content:center;height:100vh;text-align:center'><div dir='rtl'>"
                        + "<h2>ما يوصل للبيت</h2><p>تأكد من النت. دا يعيد المحاولة...</p></div></body></html>",
                        "text/html; charset=utf-8", "UTF-8");
                ui.postDelayed(() -> web.loadUrl(url), 4000);
            }

            @Override public void onPageFinished(WebView v, String u) {
                CookieManager.getInstance().flush(); // keep the login after the app closes
            }
        });
        web.setWebChromeClient(new WebChromeClient() {
            @Override public boolean onShowFileChooser(WebView v, ValueCallback<Uri[]> cb, FileChooserParams p) {
                if (pending != null) pending.onReceiveValue(null);
                pending = cb;
                try {
                    startActivityForResult(p.createIntent(), PICK_FILE);
                } catch (Exception e) {
                    pending = null;
                    return false;
                }
                return true;
            }
        });
        root.addView(web, new FrameLayout.LayoutParams(-1, -1));

        TextView gear = new TextView(this);
        gear.setText("⚙");
        gear.setTextSize(20);
        gear.setTextColor(Color.argb(120, 255, 255, 255));
        gear.setPadding(24, 12, 24, 12);
        gear.setOnClickListener(v -> web.loadUrl(url));
        gear.setOnLongClickListener(v -> {
            prefs.edit().remove("url").apply();
            showSetup();
            return true;
        });
        root.addView(gear, new FrameLayout.LayoutParams(-2, -2, Gravity.BOTTOM | Gravity.START));
        setContentView(root);
        web.loadUrl(url);
    }

    private TextView text(String s, int size, String color) {
        TextView t = new TextView(this);
        t.setText(s);
        t.setTextSize(size);
        t.setTextColor(Color.parseColor(color));
        t.setPadding(0, 12, 0, 12);
        return t;
    }

    @Override protected void onActivityResult(int req, int res, Intent data) {
        super.onActivityResult(req, res, data);
        if (req == PICK_FILE && pending != null) {
            pending.onReceiveValue(WebChromeClient.FileChooserParams.parseResult(res, data));
            pending = null;
        }
    }

    @Override public void onBackPressed() {
        if (web != null && web.canGoBack()) web.goBack(); else super.onBackPressed();
    }
}
