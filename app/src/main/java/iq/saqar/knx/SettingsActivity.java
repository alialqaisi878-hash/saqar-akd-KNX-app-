package iq.saqar.knx;

import android.app.Activity;
import android.content.Intent;
import android.graphics.Color;
import android.net.Uri;
import android.os.Bundle;
import android.text.InputType;
import android.view.Gravity;
import android.view.View;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONObject;

import java.io.File;

/** First-run / installer screen: house name, IP router address, password. */
public class SettingsActivity extends Activity {
    private static final int PICK_PROJECT = 9;
    private EditText house, ip, pw, inst, relay, tId, tSecret, tRegion;
    private TextView link;

    @Override protected void onCreate(Bundle b) {
        super.onCreate(b);
        JSONObject c = Config.load(this);

        LinearLayout l = new LinearLayout(this);
        l.setOrientation(LinearLayout.VERTICAL);
        l.setLayoutDirection(View.LAYOUT_DIRECTION_RTL);
        l.setPadding(48, 48, 48, 48);
        l.setBackgroundColor(Color.parseColor("#0f172a"));

        TextView title = text("إعداد Saqar KNX", 24);
        l.addView(title);
        l.addView(text("هذي الإعدادات يسويها المنصّب مرة وحدة لكل بيت.", 14));

        house = field(l, "اسم البيت (إنگليزي بدون فراغات، مثلاً ahmed-villa)", c.optString("house_id"), false);
        ip = field(l, "ايبي الـ IP Router بالبيت (مثلاً 192.168.1.230)", c.optString("gateway_ip"), false);
        Button find = new Button(this);
        find.setText("ابحث عن الراوتر");
        find.setOnClickListener(v -> findRouter(find));
        l.addView(find);
        pw = field(l, "باسورد الزبون للرابط من برة البيت", c.optString("remote_password"), false);
        inst = field(l, "باسورد الفني (للتعديل وبرمجة ETS، لا تنطيه للزبون)", c.optString("installer_password"), false);
        relay = field(l, "سيرفر الشركة (لا تغيّره)", c.optString("relay_url", Config.DEFAULT_RELAY), false);

        TextView th = text("أجهزة تويا (اختياري)", 19);
        th.setPadding(0, 40, 0, 4);
        l.addView(th);
        l.addView(text("من platform.tuya.com: المشروع ← Overview. اتركهن فارغات إذا البيت ما بيه تويا.", 13));
        tId = field(l, "Access ID / Client ID", c.optString("tuya_access_id"), false);
        tSecret = field(l, "Access Secret / Client Secret", c.optString("tuya_access_secret"), true);
        tRegion = field(l, "الداتاسنتر (eu لأوروبا)", c.optString("tuya_region", "eu"), false);

        link = text("", 15);
        link.setTextColor(Color.parseColor("#7dd3fc"));
        l.addView(link);
        updateLink();
        house.setOnFocusChangeListener((v, f) -> updateLink());

        Button save = new Button(this);
        save.setText("حفظ وتشغيل");
        save.setOnClickListener(v -> save());
        l.addView(save);

        Button imp = new Button(this);
        imp.setText("استرجاع نسخة احتياطية (Download/KNXProg) أو استيراد ملف");
        imp.setOnClickListener(v -> {
            Intent i = new Intent(Intent.ACTION_OPEN_DOCUMENT);
            i.addCategory(Intent.CATEGORY_OPENABLE);
            i.setType("*/*");
            startActivityForResult(i, PICK_PROJECT);
        });
        l.addView(imp);

        ScrollView sv = new ScrollView(this);
        sv.setBackgroundColor(Color.parseColor("#0f172a"));
        sv.addView(l);
        setContentView(sv);
    }

    private TextView text(String s, int size) {
        TextView t = new TextView(this);
        t.setText(s);
        t.setTextSize(size);
        t.setTextColor(Color.parseColor("#e2e8f0"));
        t.setGravity(Gravity.START);
        t.setPadding(0, 16, 0, 16);
        return t;
    }

    private EditText field(LinearLayout l, String label, String value, boolean secret) {
        l.addView(text(label, 15));
        EditText e = new EditText(this);
        e.setText(value);
        e.setTextColor(Color.WHITE);
        e.setSingleLine(true);
        e.setInputType(InputType.TYPE_CLASS_TEXT | (secret ? InputType.TYPE_TEXT_VARIATION_VISIBLE_PASSWORD : InputType.TYPE_TEXT_VARIATION_URI));
        e.setTextDirection(View.TEXT_DIRECTION_LTR);
        l.addView(e);
        return e;
    }

    private void updateLink() {
        String h = house.getText().toString().trim().toLowerCase();
        link.setText(h.isEmpty() ? "" : "رابط البيت: " + Config.PUBLIC_BASE + h + "/");
    }

    private void save() {
        String h = house.getText().toString().trim().toLowerCase();
        String i = ip.getText().toString().trim();
        String p = pw.getText().toString().trim();
        String ins = inst.getText().toString().trim();
        String r = relay.getText().toString().trim();
        if (!h.matches("[a-z0-9_-]{3,40}")) { toast("اسم البيت: حروف إنگليزية وأرقام و - فقط"); return; }
        if (!i.matches("\\d{1,3}(\\.\\d{1,3}){3}")) { toast("الايبي مو صحيح"); return; }
        if (p.length() < 6) { toast("الباسورد لازم 6 أحرف أو أكثر"); return; }
        if (ins.length() < 6) { toast("باسورد الفني لازم 6 أحرف أو أكثر"); return; }
        if (ins.equals(p)) { toast("باسورد الفني لازم يختلف عن باسورد الزبون"); return; }
        if (r.isEmpty()) r = Config.DEFAULT_RELAY;
        try {
            Config.save(this, h, i, p, ins, r, tId.getText().toString().trim(),
                    tSecret.getText().toString().trim(), tRegion.getText().toString().trim().toLowerCase());
        } catch (Exception e) {
            toast("ما انحفظ: " + e.getMessage());
            return;
        }
        restartApp();
    }

    @Override protected void onActivityResult(int req, int res, Intent data) {
        super.onActivityResult(req, res, data);
        if (req != PICK_PROJECT || res != RESULT_OK || data == null) return;
        try {
            Uri u = data.getData();
            String txt = Config.readAll(getContentResolver().openInputStream(u));
            JSONObject o = new JSONObject(txt); // must be valid JSON
            JSONObject restored = Backup.restore(this, o);
            if (restored != null) {
                house.setText(restored.optString("house_id"));
                ip.setText(restored.optString("gateway_ip"));
                pw.setText(restored.optString("remote_password"));
                inst.setText(restored.optString("installer_password"));
                relay.setText(restored.optString("relay_url", Config.DEFAULT_RELAY));
                tId.setText(restored.optString("tuya_access_id"));
                tSecret.setText(restored.optString("tuya_access_secret"));
                tRegion.setText(restored.optString("tuya_region", "eu"));
                updateLink();
                toast("انسترجع البيت والخريطة، اضغط حفظ وتشغيل");
            } else if (o.has("house_id")) {
                // settings of an existing house (keeps its house_key, so the same link keeps working)
                o.put("open_browser", false);
                Config.copy(new java.io.ByteArrayInputStream(o.toString(2).getBytes("UTF-8")), Config.file(this));
                house.setText(o.optString("house_id"));
                ip.setText(o.optString("gateway_ip"));
                pw.setText(o.optString("remote_password"));
                inst.setText(o.optString("installer_password"));
                relay.setText(o.optString("relay_url", Config.DEFAULT_RELAY));
                tId.setText(o.optString("tuya_access_id"));
                tSecret.setText(o.optString("tuya_access_secret"));
                tRegion.setText(o.optString("tuya_region", "eu"));
                updateLink();
                toast("تم استيراد إعدادات البيت، اضغط حفظ وتشغيل");
            } else {
                Config.copy(new java.io.ByteArrayInputStream(txt.getBytes("UTF-8")),
                        new File(Config.dataDir(this), "project.json"));
                toast("تم استيراد المشروع");
            }
        } catch (Exception e) {
            toast("الملف مو مشروع صحيح");
        }
    }

    /** Settings are read when the bridge starts, so restart the whole app process. */
    private void restartApp() {
        Intent i = getPackageManager().getLaunchIntentForPackage(getPackageName());
        Intent main = Intent.makeRestartActivityTask(i.getComponent());
        startActivity(main);
        Runtime.getRuntime().exit(0);
    }

    private void findRouter(Button btn) {
        btn.setEnabled(false);
        btn.setText("جاري البحث...");
        android.net.wifi.WifiManager wm = (android.net.wifi.WifiManager) getApplicationContext().getSystemService(WIFI_SERVICE);
        android.net.wifi.WifiManager.MulticastLock lock = wm != null ? wm.createMulticastLock("saqar:find") : null;
        if (lock != null) lock.acquire();
        new Thread(() -> {
            java.util.List<KnxFinder.Found> found = KnxFinder.search(3500);
            if (lock != null && lock.isHeld()) lock.release();
            runOnUiThread(() -> {
                btn.setEnabled(true);
                btn.setText("ابحث عن الراوتر");
                if (found.isEmpty()) {
                    toast("ما لگيت راوتر. تأكد إن الآيباد على واي فاي البيت والـ IP Router مربوط بنفس الشبكة");
                } else if (found.size() == 1) {
                    ip.setText(found.get(0).ip);
                    toast("لگيت: " + found.get(0).name + "  " + found.get(0).ip);
                } else {
                    String[] items = new String[found.size()];
                    for (int i = 0; i < items.length; i++) items[i] = found.get(i).name + "   " + found.get(i).ip;
                    new android.app.AlertDialog.Builder(this)
                            .setTitle("اختار الراوتر")
                            .setItems(items, (d, w) -> ip.setText(found.get(w).ip))
                            .show();
                }
            });
        }).start();
    }

    private void toast(String s) { Toast.makeText(this, s, Toast.LENGTH_LONG).show(); }
}
