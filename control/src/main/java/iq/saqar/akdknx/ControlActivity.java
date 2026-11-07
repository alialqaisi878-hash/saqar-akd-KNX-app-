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
    private static final String DEMO = "https://knx.saqar-akd.com.iq/demo/?app=tat";
    private static final String LOCAL = "http://127.0.0.1:8080/";
    private static final int PICK_FILE = 7;
    private SharedPreferences prefs;
    private WebView web;
    private ValueCallback<Uri[]> pending;
    private final Handler ui = new Handler(Looper.getMainLooper());

    @Override protected void onCreate(Bundle b) {
        super.onCreate(b);
        prefs = getSharedPreferences("akd", MODE_PRIVATE);
        edgeToEdge();
        if (prefs.getString("url", "").isEmpty()) showSetup(); else showPanel();
        if (!"play".equals(BuildConfig.STORE)) Updater.check((code, name, err) -> {
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

    /** Android 15+ draws under the status and navigation bars: keep the page clear of them (and of the keyboard). */
    private void edgeToEdge() {
        if (android.os.Build.VERSION.SDK_INT < 30) return;
        View content = findViewById(android.R.id.content);
        content.setOnApplyWindowInsetsListener((v, in) -> {
            android.graphics.Insets s = in.getInsets(android.view.WindowInsets.Type.systemBars()
                    | android.view.WindowInsets.Type.displayCutout() | android.view.WindowInsets.Type.ime());
            v.setPadding(s.left, s.top, s.right, s.bottom);
            return in;
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
        Button demo = new Button(this);
        demo.setText("جرّب بيت تجريبي (عرض)");
        demo.setOnClickListener(v -> {
            prefs.edit().putString("house", "demo").putBoolean("local", false).putString("url", DEMO).apply();
            showPanel();
        });
        l.addView(demo);
        l.addView(text("للتجربة بدون بيت. حتى ترجع: اضغط مطوّل على ⚙.", 12, "#64748b"));
        setContentView(l);
    }

    @SuppressLint("SetJavaScriptEnabled")
    private void showPanel() {
        final String url = prefs.getString("url", "");
        boolean isLocal = prefs.getBoolean("local", false);
        if (isLocal) getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);

        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(Color.parseColor("#edf1f6"));
        web = new WebView(this);
        web.getSettings().setJavaScriptEnabled(true);
        web.getSettings().setDomStorageEnabled(true);
        // tells the page it runs inside TAT SMART: customer controls only, no technician login
        web.getSettings().setUserAgentString(web.getSettings().getUserAgentString() + " TATSMART");
        web.addJavascriptInterface(new Js(), "TATApp");
        CookieManager.getInstance().setAcceptCookie(true);
        CookieManager.getInstance().setAcceptThirdPartyCookies(web, true);
        web.setWebViewClient(new WebViewClient() {
            @Override public void onReceivedError(WebView v, WebResourceRequest r, WebResourceError e) {
                if (!r.isForMainFrame()) return;
                v.loadData("<html><body style='background:#edf1f6;color:#0f1a28;font-family:sans-serif;display:flex;"
                        + "align-items:center;justify-content:center;height:100vh;text-align:center'><div dir='rtl'>"
                        + "<h2>ما يوصل للبيت</h2><p>تأكد من النت. دا يعيد المحاولة...</p></div></body></html>",
                        "text/html; charset=utf-8", "UTF-8");
                ui.postDelayed(() -> openBest(true), 4000);
            }

            // links that leave the house (Spotify, web pages) open in their own app, not inside TAT SMART
            @Override public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest r) {
                Uri u = r.getUrl();
                String cur = v.getUrl();
                String host = u.getHost() == null ? "" : u.getHost();
                boolean http = "http".equals(u.getScheme()) || "https".equals(u.getScheme());
                if (http && (r.isRedirect() || host.endsWith("saqar-akd.com.iq") || (cur != null && host.equals(Uri.parse(cur).getHost())))) return false;
                try {
                    startActivity(new Intent(Intent.ACTION_VIEW, u).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
                } catch (Exception e) {
                    Toast.makeText(ControlActivity.this, "ما كدر يفتح الرابط", Toast.LENGTH_SHORT).show();
                }
                return true;
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
        gear.setOnClickListener(v -> openBest(true));
        gear.setOnLongClickListener(v -> {
            prefs.edit().remove("url").apply();
            showSetup();
            return true;
        });
        root.addView(gear, new FrameLayout.LayoutParams(-2, -2, Gravity.BOTTOM | Gravity.START));
        setContentView(root);
        openBest(true);
    }

    private String current = "";

    /** At home on the same Wi-Fi: talk to the house tablet directly (fast). Away: through the company server. */
    private void openBest(boolean force) {
        final String remote = prefs.getString("url", "");
        if (prefs.getBoolean("local", false)) { if (force || current.isEmpty()) { current = remote; web.loadUrl(remote); } return; }
        final String lans = prefs.getString("lan", "");
        new Thread(() -> {
            String pick = remote;
            try {
                org.json.JSONArray a = new org.json.JSONArray(lans.isEmpty() ? "[]" : lans);
                for (int i = 0; i < a.length(); i++) {
                    String u = a.getString(i);
                    try {
                        HttpURLConnection h = (HttpURLConnection) new URL(u + "ping").openConnection();
                        h.setConnectTimeout(700);
                        h.setReadTimeout(700);
                        if (h.getResponseCode() == 200) { pick = u; break; }
                    } catch (Exception ignored) { }
                }
            } catch (Exception ignored) { }
            final String go = pick;
            runOnUiThread(() -> {
                if (!force && go.equals(current)) return;
                current = go;
                web.loadUrl(go);
                if (!go.equals(remote)) Toast.makeText(this, "🏠 متصل مباشرة بالبيت (أسرع)", Toast.LENGTH_SHORT).show();
            });
        }).start();
    }

    @Override protected void onResume() {
        super.onResume();
        if (web != null && !current.isEmpty()) openBest(false);
        if (Push.isOn(this)) { Push.start(this); keepAlertsAlive(); }
    }

    /** What the house page may ask the app to do. */
    private class Js {
        @android.webkit.JavascriptInterface public void setLan(String json) {
            if (!json.equals(prefs.getString("lan", ""))) prefs.edit().putString("lan", json).apply();
        }
        @android.webkit.JavascriptInterface public void geo(String json) {
            prefs.edit().putString("geo", json).remove("geo_last").apply();
            runOnUiThread(ControlActivity.this::askLocation);
        }
        @android.webkit.JavascriptInterface public void geoOff() {
            Geo.disable(ControlActivity.this);
            runOnUiThread(() -> Toast.makeText(ControlActivity.this, "انطفى الوصول التلقائي", Toast.LENGTH_SHORT).show());
        }
        /** House alerts on this phone (ntfy topic of the house). */
        @android.webkit.JavascriptInterface public void push(String topic, boolean on) {
            runOnUiThread(() -> {
                if (on && android.os.Build.VERSION.SDK_INT >= 33
                        && checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS) != android.content.pm.PackageManager.PERMISSION_GRANTED)
                    requestPermissions(new String[]{android.Manifest.permission.POST_NOTIFICATIONS}, 53);
                Push.set(ControlActivity.this, topic == null ? "" : topic.trim(), on);
                if (on) keepAlertsAlive();
            });
        }
        @android.webkit.JavascriptInterface public String pushState() {
            if (!Push.isOn(ControlActivity.this)) return "off";
            if (android.os.Build.VERSION.SDK_INT >= 33
                    && checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS) != android.content.pm.PackageManager.PERMISSION_GRANTED) return "need_permission";
            return "on";
        }
        @android.webkit.JavascriptInterface public String geoState() {
            return prefs.getString("geo", "").isEmpty() ? "off" : Geo.hasPermission(ControlActivity.this) ? "on" : "need_permission";
        }
    }

    /** Alerts must arrive with the app closed: ask once to be left out of battery saving, and on
     *  Honor / Huawei / Xiaomi phones also open their own "auto launch" screen (they kill closed apps). */
    private void keepAlertsAlive() {
        if (prefs.getBoolean("alive_asked", false)) return;
        prefs.edit().putBoolean("alive_asked", true).apply();
        android.os.PowerManager pm = getSystemService(android.os.PowerManager.class);
        boolean exempt = pm != null && pm.isIgnoringBatteryOptimizations(getPackageName());
        String maker = android.os.Build.MANUFACTURER.toLowerCase();
        final boolean strict = maker.contains("honor") || maker.contains("huawei") || maker.contains("xiaomi") || maker.contains("redmi") || maker.contains("oppo") || maker.contains("vivo") || maker.contains("realme");
        if (exempt && !strict) return;
        String msg = "حتى توصلك تنبيهات البيت والتطبيق مسدود، اسمح لـ TAT SMART يشتغل بالخلفية."
                + (strict ? "\n\nبالشاشة الجاية فعّل TAT SMART، أو اختار (إدارة يدوية) وشغّل: التشغيل التلقائي، التشغيل الثانوي، والتشغيل بالخلفية." : "");
        new android.app.AlertDialog.Builder(this)
                .setTitle("تنبيهات البيت")
                .setMessage(msg)
                .setPositiveButton("تمام", (d, w) -> {
                    if (!exempt) {
                        try {
                            if ("play".equals(BuildConfig.STORE)) startActivity(new Intent(android.provider.Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS));
                            else startActivity(new Intent(android.provider.Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:" + getPackageName())));
                        } catch (Exception ignored) { }
                    }
                    if (strict) web.postDelayed(this::openAutoStart, exempt ? 0 : 1500);
                })
                .setNegativeButton("بعدين", null)
                .show();
    }

    private void openAutoStart() {
        String[][] screens = {
                {"com.hihonor.systemmanager", "com.hihonor.systemmanager.startupmgr.ui.StartupNormalAppListActivity"},
                {"com.huawei.systemmanager", "com.huawei.systemmanager.startupmgr.ui.StartupNormalAppListActivity"},
                {"com.huawei.systemmanager", "com.huawei.systemmanager.optimize.process.ProtectActivity"},
                {"com.miui.securitycenter", "com.miui.permcenter.autostart.AutoStartManagementActivity"},
                {"com.coloros.safecenter", "com.coloros.safecenter.permission.startup.StartupAppListActivity"},
                {"com.vivo.permissionmanager", "com.vivo.permissionmanager.activity.BgStartUpManagerActivity"},
        };
        for (String[] s : screens) {
            try {
                startActivity(new Intent().setComponent(new android.content.ComponentName(s[0], s[1])).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
                return;
            } catch (Exception ignored) { }
        }
        try {
            startActivity(new Intent(android.provider.Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:" + getPackageName())));
        } catch (Exception ignored) { }
    }

    private void askLocation() {
        if (!Geo.hasPermission(this)) {
            requestPermissions(new String[]{android.Manifest.permission.ACCESS_FINE_LOCATION, android.Manifest.permission.ACCESS_COARSE_LOCATION}, 51);
            return;
        }
        if (android.os.Build.VERSION.SDK_INT >= 29 && checkSelfPermission(android.Manifest.permission.ACCESS_BACKGROUND_LOCATION) != android.content.pm.PackageManager.PERMISSION_GRANTED) {
            new android.app.AlertDialog.Builder(this)
                    .setTitle("إذن الموقع طول الوقت")
                    .setMessage("حتى البيت يعرف من توصل حتى لو التطبيق مسدود، اختار بالشاشة الجاية: \"السماح طوال الوقت\".")
                    .setPositiveButton("تمام", (d, w) -> requestPermissions(new String[]{android.Manifest.permission.ACCESS_BACKGROUND_LOCATION}, 52))
                    .show();
            return;
        }
        boolean ok = Geo.register(this);
        Toast.makeText(this, ok ? "✓ الوصول التلقائي شغّال على هذا التلفون" : "ما تفعّل، تأكد من إذن الموقع", Toast.LENGTH_LONG).show();
    }

    @Override public void onRequestPermissionsResult(int code, String[] perms, int[] res) {
        super.onRequestPermissionsResult(code, perms, res);
        boolean granted = res.length > 0 && res[0] == android.content.pm.PackageManager.PERMISSION_GRANTED;
        if (code == 51 && granted) askLocation();
        else if (code == 52) {
            boolean ok = Geo.register(this);
            Toast.makeText(this, granted && ok ? "✓ الوصول التلقائي شغّال على هذا التلفون"
                    : ok ? "شغّال بس لما التطبيق مفتوح. حتى يشتغل دايماً: الإعدادات ← التطبيقات ← TAT SMART ← الموقع ← السماح طوال الوقت"
                    : "ما تفعّل: لازم إذن الموقع", Toast.LENGTH_LONG).show();
        } else Toast.makeText(this, "ما تفعّل: لازم إذن الموقع", Toast.LENGTH_LONG).show();
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
