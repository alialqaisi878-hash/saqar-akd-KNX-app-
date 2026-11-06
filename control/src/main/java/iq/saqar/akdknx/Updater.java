package iq.saqar.akdknx;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageInfo;
import android.content.pm.PackageInstaller;
import android.os.Build;

import org.json.JSONObject;

import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;

/**
 * Self update from the company build (apk branch of the GitHub repo).
 * Android always asks the person on the device to confirm the install; we never install silently.
 */
public class Updater extends BroadcastReceiver {
    private static final String BASE = "https://raw.githubusercontent.com/alialqaisi878-hash/saqar-akd-KNX-app-/apk/";
    private static final String RELEASE = "https://knx.saqar-akd.com.iq/release.json";
    private static final String KEY = "tatsmart";   // entry in version.json
    private static final String FILE = "TATSMART.apk"; // apk name on the apk branch
    private static final String CH = "update";
    private static volatile boolean busy = false;

    public interface Result { void done(int remoteCode, String remoteName, String err); }

    static int localCode(Context c) {
        try {
            PackageInfo pi = c.getPackageManager().getPackageInfo(c.getPackageName(), 0);
            return Build.VERSION.SDK_INT >= 28 ? (int) pi.getLongVersionCode() : pi.versionCode;
        } catch (Exception e) { return 0; }
    }

    /** Version the company released in TAT Admin (server release.json); 0 = no gate yet, -1 = server not reachable. */
    static int released() {
        try {
            HttpURLConnection c = (HttpURLConnection) new URL(RELEASE + "?t=" + System.currentTimeMillis()).openConnection();
            c.setConnectTimeout(8000);
            c.setReadTimeout(8000);
            if (c.getResponseCode() == 404) return 0;
            StringBuilder sb = new StringBuilder();
            try (InputStream in = c.getInputStream()) {
                byte[] b = new byte[4096];
                int n;
                while ((n = in.read(b)) > 0) sb.append(new String(b, 0, n, "UTF-8"));
            }
            return new JSONObject(sb.toString()).optInt(KEY, 0);
        } catch (Exception e) {
            return -1;  // server not reachable: wait for the next check
        }
    }

    /** Reads version.json; calls back on the same background thread. */
    static void check(Result r) {
        new Thread(() -> {
            try {
                HttpURLConnection c = (HttpURLConnection) new URL(BASE + "version.json?t=" + System.currentTimeMillis()).openConnection();
                c.setConnectTimeout(8000);
                c.setReadTimeout(8000);
                StringBuilder sb = new StringBuilder();
                try (InputStream in = c.getInputStream()) {
                    byte[] b = new byte[4096];
                    int n;
                    while ((n = in.read(b)) > 0) sb.append(new String(b, 0, n, "UTF-8"));
                }
                JSONObject o = new JSONObject(sb.toString()).getJSONObject(KEY);
                int code = o.optInt("code");
                int rel = released();
                if (rel < 0 || (rel > 0 && code > rel)) code = 0;  // not released by the company yet (or server not reachable)
                r.done(code, o.optString("name"), null);
            } catch (Exception e) {
                r.done(0, "", String.valueOf(e.getMessage()));
            }
        }).start();
    }

    /** Downloads the new APK and hands it to Android's installer (which asks for confirmation). */
    static void install(Context ctx, Result r) {
        if (busy) return;
        busy = true;
        Context c = ctx.getApplicationContext();
        new Thread(() -> {
            try {
                PackageInstaller pi = c.getPackageManager().getPackageInstaller();
                PackageInstaller.SessionParams p = new PackageInstaller.SessionParams(PackageInstaller.SessionParams.MODE_FULL_INSTALL);
                int id = pi.createSession(p);
                try (PackageInstaller.Session s = pi.openSession(id)) {
                    HttpURLConnection h = (HttpURLConnection) new URL(BASE + FILE + "?t=" + System.currentTimeMillis()).openConnection();
                    h.setConnectTimeout(15000);
                    h.setReadTimeout(60000);
                    try (InputStream in = h.getInputStream(); OutputStream out = s.openWrite("app.apk", 0, -1)) {
                        byte[] b = new byte[65536];
                        int n;
                        while ((n = in.read(b)) > 0) out.write(b, 0, n);
                        s.fsync(out);
                    }
                    Intent i = new Intent(c, Updater.class);
                    int fl = PendingIntent.FLAG_UPDATE_CURRENT | (Build.VERSION.SDK_INT >= 31 ? PendingIntent.FLAG_MUTABLE : 0);
                    s.commit(PendingIntent.getBroadcast(c, id, i, fl).getIntentSender());
                }
                if (r != null) r.done(0, "", null);
            } catch (Exception e) {
                if (r != null) r.done(0, "", String.valueOf(e.getMessage()));
            } finally {
                busy = false;
            }
        }).start();
    }

    @Override public void onReceive(Context c, Intent intent) {
        int st = intent.getIntExtra(PackageInstaller.EXTRA_STATUS, PackageInstaller.STATUS_FAILURE);
        if (st != PackageInstaller.STATUS_PENDING_USER_ACTION) return;
        Intent confirm = intent.getParcelableExtra(Intent.EXTRA_INTENT);
        if (confirm == null) return;
        confirm.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        try { c.startActivity(confirm); } catch (Exception ignored) { }
        // also leave a notification, in case Android blocks opening the screen from the background
        NotificationManager nm = c.getSystemService(NotificationManager.class);
        if (nm == null) return;
        if (Build.VERSION.SDK_INT >= 26) nm.createNotificationChannel(new NotificationChannel(CH, "تحديثات", NotificationManager.IMPORTANCE_HIGH));
        PendingIntent pi = PendingIntent.getActivity(c, 9, confirm, PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        Notification n = new Notification.Builder(c, CH)
                .setSmallIcon(android.R.drawable.stat_sys_download_done)
                .setContentTitle("نسخة جديدة جاهزة")
                .setContentText("دوس هنا حتى تثبّت التحديث")
                .setContentIntent(pi)
                .setAutoCancel(true)
                .build();
        nm.notify(77, n);
    }
}
