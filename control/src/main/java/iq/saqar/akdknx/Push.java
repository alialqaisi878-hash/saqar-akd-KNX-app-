package iq.saqar.akdknx;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.IBinder;
import android.util.Log;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URLEncoder;

/**
 * House alerts on this phone, also when TAT SMART is closed.
 * The house tablet posts alerts to the house's private ntfy topic; this service keeps one
 * light connection to it and shows each alert as a normal Android notification.
 */
public class Push extends Service {
    private static final String TAG = "TATPush";
    static final String CH_ALERT = "tat_alerts";
    static final String CH_SVC = "tat_service";
    private volatile boolean running;
    private Thread worker;

    static SharedPreferences prefs(Context c) { return c.getSharedPreferences("tat", MODE_PRIVATE); }

    /** Called by the house page: topic of this house, on/off. */
    static void set(Context c, String topic, boolean on) {
        SharedPreferences p = prefs(c);
        boolean changed = !topic.equals(p.getString("push_topic", ""));
        SharedPreferences.Editor e = p.edit().putString("push_topic", topic).putBoolean("push_on", on);
        if (changed) e.remove("push_since");
        e.apply();
        if (on && !topic.isEmpty()) start(c);
        else c.stopService(new Intent(c, Push.class));
    }

    static boolean isOn(Context c) {
        SharedPreferences p = prefs(c);
        return p.getBoolean("push_on", false) && !p.getString("push_topic", "").isEmpty();
    }

    static void start(Context c) {
        if (!isOn(c)) return;
        try {
            Intent i = new Intent(c, Push.class);
            if (Build.VERSION.SDK_INT >= 26) c.startForegroundService(i); else c.startService(i);
        } catch (Exception e) {
            Log.w(TAG, "cannot start alerts", e);
        }
    }

    /** Wake up again after delayMs (also in Doze) and make sure the service is running. */
    static void watchdog(Context c, long delayMs) {
        try {
            android.app.AlarmManager am = c.getSystemService(android.app.AlarmManager.class);
            PendingIntent pi = PendingIntent.getBroadcast(c, 77, new Intent(c, PushKick.class),
                    PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
            if (!isOn(c)) { am.cancel(pi); return; }
            long at = android.os.SystemClock.elapsedRealtime() + delayMs;
            if (Build.VERSION.SDK_INT >= 23) am.setAndAllowWhileIdle(android.app.AlarmManager.ELAPSED_REALTIME_WAKEUP, at, pi);
            else am.set(android.app.AlarmManager.ELAPSED_REALTIME_WAKEUP, at, pi);
        } catch (Exception e) {
            Log.w(TAG, "watchdog", e);
        }
    }

    static void channels(Context c) {
        if (Build.VERSION.SDK_INT < 26) return;
        NotificationManager nm = c.getSystemService(NotificationManager.class);
        NotificationChannel a = new NotificationChannel(CH_ALERT, "تنبيهات البيت", NotificationManager.IMPORTANCE_HIGH);
        a.setDescription("وصول، طاقة، حرارة، وكل تنبيه يدزه البيت");
        nm.createNotificationChannel(a);
        NotificationChannel s = new NotificationChannel(CH_SVC, "اتصال التنبيهات", NotificationManager.IMPORTANCE_MIN);
        s.setDescription("يبقي التنبيهات توصل والتطبيق مسدود. تكدر تخفي هذا الإشعار من هنا.");
        s.setShowBadge(false);
        nm.createNotificationChannel(s);
    }

    private PendingIntent openApp() {
        Intent i = new Intent(this, ControlActivity.class).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        return PendingIntent.getActivity(this, 0, i, PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
    }

    private Notification.Builder builder(String ch) {
        return Build.VERSION.SDK_INT >= 26 ? new Notification.Builder(this, ch) : new Notification.Builder(this);
    }

    @Override public void onCreate() {
        super.onCreate();
        channels(this);
        Notification n = builder(CH_SVC)
                .setSmallIcon(R.drawable.ic_tat)
                .setContentTitle("TAT SMART")
                .setContentText("تنبيهات البيت شغّالة")
                .setContentIntent(openApp())
                .setOngoing(true)
                .build();
        if (Build.VERSION.SDK_INT >= 34) startForeground(7, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);
        else startForeground(7, n);
    }

    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        if (!isOn(this)) { stopSelf(); return START_NOT_STICKY; }
        if (worker == null || !worker.isAlive()) {
            running = true;
            worker = new Thread(this::loop, "tat-push");
            worker.start();
        }
        watchdog(this, 15 * 60 * 1000L);
        return START_STICKY;
    }

    private void loop() {
        int wait = 3;
        while (running && isOn(this)) {
            HttpURLConnection con = null;
            try {
                SharedPreferences p = prefs(this);
                String topic = URLEncoder.encode(p.getString("push_topic", ""), "UTF-8");
                String since = p.getString("push_since", "");
                String url = "https://ntfy.sh/" + topic + "/json" + (since.isEmpty() ? "" : "?since=" + URLEncoder.encode(since, "UTF-8"));
                con = (HttpURLConnection) new URL(url).openConnection();
                con.setConnectTimeout(15000);
                con.setReadTimeout(120000);  // ntfy sends a keepalive every 45 s
                con.setRequestProperty("User-Agent", "TATSMART");
                if (con.getResponseCode() != 200) throw new java.io.IOException("HTTP " + con.getResponseCode());
                BufferedReader in = new BufferedReader(new InputStreamReader(con.getInputStream(), "UTF-8"));
                wait = 3;
                String line;
                while (running && (line = in.readLine()) != null) {
                    if (line.isEmpty()) continue;
                    JSONObject m = new JSONObject(line);
                    if (!"message".equals(m.optString("event"))) continue;
                    String id = m.optString("id");
                    if (!id.isEmpty()) prefs(this).edit().putString("push_since", id).apply();
                    show(m.optString("title", "TAT SMART"), m.optString("message", ""), id.hashCode());
                }
            } catch (Exception e) {
                Log.w(TAG, "alerts connection: " + e);
            } finally {
                if (con != null) con.disconnect();
            }
            try { Thread.sleep(wait * 1000L); } catch (InterruptedException e) { return; }
            wait = Math.min(wait * 2, 60);
        }
        stopSelf();
    }

    private void show(String title, String body, int id) {
        Notification n = builder(CH_ALERT)
                .setSmallIcon(R.drawable.ic_tat)
                .setContentTitle(title.isEmpty() ? "TAT SMART" : title)
                .setContentText(body)
                .setStyle(new Notification.BigTextStyle().bigText(body))
                .setContentIntent(openApp())
                .setAutoCancel(true)
                .build();
        getSystemService(NotificationManager.class).notify(1000 + (id & 0xffff), n);
    }

    /** The app was swiped away from recent apps: some phones (Honor, Huawei, Xiaomi) stop us with it. Come back. */
    @Override public void onTaskRemoved(Intent rootIntent) {
        watchdog(this, 2000);
        super.onTaskRemoved(rootIntent);
    }

    @Override public void onDestroy() {
        if (isOn(this)) watchdog(this, 5000);
        running = false;
        if (worker != null) worker.interrupt();
        super.onDestroy();
    }

    @Override public IBinder onBind(Intent intent) { return null; }
}
