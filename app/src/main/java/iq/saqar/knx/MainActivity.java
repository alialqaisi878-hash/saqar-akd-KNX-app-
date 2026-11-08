package iq.saqar.knx;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Intent;
import android.graphics.Color;
import android.graphics.drawable.GradientDrawable;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.PowerManager;
import android.provider.Settings;
import android.text.InputType;
import android.view.View;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONObject;

import java.net.HttpURLConnection;
import java.net.URL;

/** KNX Prog: the bridge's own screen. Shows whether the house is connected; settings need the installer password. */
public class MainActivity extends Activity {
    private final Handler ui = new Handler(Looper.getMainLooper());
    private TextView knx, relay, tuya, link, house;
    private String linkText = "";
    private final Runnable poll = this::refresh;

    @Override protected void onCreate(Bundle b) {
        super.onCreate(b);
        if (!Config.isConfigured(this)) {
            startActivity(new Intent(this, SettingsActivity.class));
            finish();
            return;
        }
        BridgeService.start(this);
        askPermissions();

        LinearLayout l = new LinearLayout(this);
        l.setOrientation(LinearLayout.VERTICAL);
        l.setLayoutDirection(View.LAYOUT_DIRECTION_RTL);
        l.setPadding(48, 64, 48, 48);
        l.setBackgroundColor(Color.parseColor("#0f172a"));

        l.addView(text("KNX Prog", 28, "#7dd3fc"));
        l.addView(text("جسر المنظومة. خلّي هذا الجهاز شغّال ومتصل بالنت دايماً.", 14, "#94a3b8"));
        house = text("", 18, "#e2e8f0");
        l.addView(house);
        knx = row(l, "الـ IP Router");
        relay = row(l, "سيرفر الشركة");
        tuya = row(l, "تويا");
        link = text("", 15, "#7dd3fc");
        link.setTextDirection(View.TEXT_DIRECTION_LTR);
        link.setPadding(0, 24, 0, 8);
        link.setOnClickListener(v -> {
            if (linkText.isEmpty()) return;
            ((ClipboardManager) getSystemService(CLIPBOARD_SERVICE)).setPrimaryClip(ClipData.newPlainText("link", linkText));
            Toast.makeText(this, "تم نسخ الرابط", Toast.LENGTH_SHORT).show();
        });
        l.addView(link);
        l.addView(text("اضغط على الرابط حتى تنسخه.", 12, "#64748b"));

        l.addView(text("هذا التطبيق جسر بس. التعديل والصيانة من تطبيق TAT SMART: الإعدادات ← 🔧 دخول الفني.", 13, "#94a3b8"));
        Button settings = button("الإعدادات (الفني)");
        settings.setOnClickListener(v -> openSettings());
        l.addView(settings);
        Button upd = button("تحديث KNX Prog (النسخة " + BuildInfo.name(this) + ")");
        upd.setOnClickListener(v -> {
            Toast.makeText(this, "جاري الفحص...", Toast.LENGTH_SHORT).show();
            Updater.check((code, name, err) -> runOnUiThread(() -> {
                if (err != null) { Toast.makeText(this, "ما يوصل للسيرفر: " + err, Toast.LENGTH_LONG).show(); return; }
                if (code <= Updater.localCode(this)) { Toast.makeText(this, "عندك آخر نسخة ✓", Toast.LENGTH_LONG).show(); return; }
                Toast.makeText(this, "جاري تحميل النسخة " + name + "...", Toast.LENGTH_LONG).show();
                Updater.install(this, (c2, n2, e2) -> {
                    if (e2 != null) runOnUiThread(() -> Toast.makeText(this, "ما كدر يحمّل: " + e2, Toast.LENGTH_LONG).show());
                });
            }));
        });
        l.addView(upd);

        ScrollView sv = new ScrollView(this);
        sv.setBackgroundColor(Color.parseColor("#0f172a"));
        sv.addView(l);
        setContentView(sv);
    }

    @Override protected void onResume() { super.onResume(); ui.post(poll); }
    @Override protected void onPause() { super.onPause(); ui.removeCallbacks(poll); }

    private void refresh() {
        new Thread(() -> {
            JSONObject o = null;
            try {
                HttpURLConnection c = (HttpURLConnection) new URL("http://127.0.0.1:8080/api/bridge").openConnection();
                c.setConnectTimeout(1500);
                c.setReadTimeout(1500);
                o = new JSONObject(Config.readAll(c.getInputStream()));
            } catch (Exception ignored) { }
            final JSONObject s = o;
            runOnUiThread(() -> show(s));
        }).start();
        ui.postDelayed(poll, 3000);
    }

    private void show(JSONObject s) {
        if (s == null) {
            house.setText("جاري تشغيل الجسر...");
            set(knx, "...", "#fbbf24");
            set(relay, "...", "#fbbf24");
            set(tuya, "...", "#fbbf24");
            return;
        }
        house.setText("البيت: " + s.optString("house") + "   ·   الراوتر: " + s.optString("gateway"));
        String k = s.optJSONObject("knx") != null ? s.optJSONObject("knx").optString("state") : "";
        set(knx, "connected".equals(k) ? "متصل ✓" : "connecting".equals(k) ? "جاري الاتصال..." : "غير متصل ✕",
                "connected".equals(k) ? "#4ade80" : "connecting".equals(k) ? "#fbbf24" : "#f87171");
        JSONObject r = s.optJSONObject("relay");
        String rs = r != null ? r.optString("state") : "";
        String rt = "connected".equals(rs) ? "متصل ✓" : "refused".equals(rs) ? "مرفوض: اسم البيت مستخدم" :
                "error".equals(rs) ? "ما يوصل (تأكد من النت)" : "جاري الاتصال...";
        set(relay, rt, "connected".equals(rs) ? "#4ade80" : "connecting".equals(rs) || "off".equals(rs) ? "#fbbf24" : "#f87171");
        JSONObject t = s.optJSONObject("tuya");
        String ts = t != null ? t.optString("state") : "off";
        String td = t != null ? t.optString("detail") : "";
        set(tuya, "connected".equals(ts) ? "متصل ✓ " + td.replace(" devices", " جهاز") : "off".equals(ts) ? "غير مفعّل" :
                "error".equals(ts) ? "خطأ: " + td : "جاري الاتصال...",
                "connected".equals(ts) ? "#4ade80" : "error".equals(ts) ? "#f87171" : "#94a3b8");
        linkText = s.optString("link").replace("https://srv2056159.hstgr.cloud/knx/h/", Config.PUBLIC_BASE);
        link.setText(linkText);
    }

    private TextView row(LinearLayout l, String label) {
        LinearLayout r = new LinearLayout(this);
        r.setOrientation(LinearLayout.HORIZONTAL);
        r.setPadding(32, 28, 32, 28);
        GradientDrawable bg = new GradientDrawable();
        bg.setColor(Color.parseColor("#1e293b"));
        bg.setCornerRadius(24);
        r.setBackground(bg);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(-1, -2);
        lp.topMargin = 20;
        TextView name = text(label, 16, "#e2e8f0");
        r.addView(name, new LinearLayout.LayoutParams(0, -2, 1));
        TextView v = text("...", 16, "#fbbf24");
        r.addView(v);
        l.addView(r, lp);
        return v;
    }

    private void set(TextView t, String s, String color) { t.setText(s); t.setTextColor(Color.parseColor(color)); }

    private TextView text(String s, int size, String color) {
        TextView t = new TextView(this);
        t.setText(s);
        t.setTextSize(size);
        t.setTextColor(Color.parseColor(color));
        t.setPadding(0, 8, 0, 8);
        return t;
    }

    private Button button(String s) {
        Button b = new Button(this);
        b.setText(s);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(-1, -2);
        lp.topMargin = 24;
        b.setLayoutParams(lp);
        return b;
    }

    /** The settings screen is for the installer only. */
    private void openSettings() {
        String need = Config.load(this).optString("installer_password");
        if (need.isEmpty()) { startActivity(new Intent(this, SettingsActivity.class)); return; }
        EditText e = new EditText(this);
        e.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD);
        new AlertDialog.Builder(this)
                .setTitle("باسورد الفني")
                .setView(e)
                .setPositiveButton("دخول", (d, w) -> {
                    if (e.getText().toString().trim().equals(need)) startActivity(new Intent(this, SettingsActivity.class));
                    else Toast.makeText(this, "باسورد غلط", Toast.LENGTH_SHORT).show();
                })
                .setNegativeButton("إلغاء", null)
                .show();
    }

    private void askPermissions() {
        if (Build.VERSION.SDK_INT >= 33) requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, 1);
        PowerManager pm = getSystemService(PowerManager.class);
        if (pm != null && !pm.isIgnoringBatteryOptimizations(getPackageName())) {
            try {
                startActivity(new Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                        Uri.parse("package:" + getPackageName())));
            } catch (Exception ignored) { }
        }
    }
}
