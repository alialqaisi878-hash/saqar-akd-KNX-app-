package iq.saqar.knx;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.net.wifi.WifiManager;
import android.os.Build;
import android.os.IBinder;
import android.os.PowerManager;
import android.util.Log;

import com.chaquo.python.PyObject;
import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;

import java.io.File;

/** Keeps the Python KNX bridge (knx_bridge.py) running in the background, also after reboot. */
public class BridgeService extends Service {
    private static final String TAG = "SaqarKNX";
    private static final String CH = "bridge";
    private static volatile boolean started = false;
    private PowerManager.WakeLock wake;
    private WifiManager.WifiLock wifi;
    private WifiManager.MulticastLock multicast;

    static void start(Context c) {
        if (!Config.isConfigured(c)) return;
        Intent i = new Intent(c, BridgeService.class);
        if (Build.VERSION.SDK_INT >= 26) c.startForegroundService(i); else c.startService(i);
    }

    @Override public void onCreate() {
        super.onCreate();
        NotificationManager nm = getSystemService(NotificationManager.class);
        nm.createNotificationChannel(new NotificationChannel(CH, "KNX", NotificationManager.IMPORTANCE_LOW));
        PendingIntent open = PendingIntent.getActivity(this, 0, new Intent(this, MainActivity.class),
                PendingIntent.FLAG_IMMUTABLE);
        Notification n = new Notification.Builder(this, CH)
                .setContentTitle("Saqar KNX")
                .setContentText("المنظومة شغّالة")
                .setSmallIcon(android.R.drawable.stat_sys_upload_done)
                .setContentIntent(open)
                .setOngoing(true)
                .build();
        if (Build.VERSION.SDK_INT >= 34) {
            startForeground(1, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);
        } else {
            startForeground(1, n);
        }
        PowerManager pm = getSystemService(PowerManager.class);
        wake = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "saqar:knx");
        wake.acquire();
        WifiManager wm = (WifiManager) getApplicationContext().getSystemService(Context.WIFI_SERVICE);
        if (wm != null) {
            wifi = wm.createWifiLock(WifiManager.WIFI_MODE_FULL_HIGH_PERF, "saqar:knx");
            wifi.acquire();
            multicast = wm.createMulticastLock("saqar:knx");
            multicast.setReferenceCounted(false);
            multicast.acquire();
        }
    }

    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        startBridge();
        return START_STICKY;
    }

    private synchronized void startBridge() {
        if (started) return;
        started = true;
        final Context ctx = getApplicationContext();
        Thread bk = new Thread(() -> {
            long lastCheck = 0;
            while (true) {
                Backup.run(ctx);
                // update: every 6 h, or right away when the technician asks from the maintenance page
                File req = new File(Config.dataDir(ctx), "update.request");
                boolean asked = req.exists();
                if (asked || System.currentTimeMillis() - lastCheck > 6 * 3600_000L) {
                    lastCheck = System.currentTimeMillis();
                    req.delete();
                    Updater.check((code, name, err) -> {
                        if (err == null && code > Updater.localCode(ctx)) Updater.install(ctx, null);
                    });
                }
                for (int i = 0; i < 24 && !new File(Config.dataDir(ctx), "update.request").exists(); i++) {
                    try { Thread.sleep(5_000); } catch (InterruptedException e) { return; }
                }
            }
        }, "knx-backup");
        bk.setDaemon(true);
        bk.start();
        new Thread(() -> {
            try {
                File web = new File(ctx.getFilesDir(), "web");
                if (!web.exists()) web.mkdirs();
                File keep = new File(web, "apk");  // the APK's own page, used again if an online update is rolled back
                if (!keep.exists()) keep.mkdirs();
                for (String f : new String[]{"index.html", "three.js", "tat-180.png", "tat-192.png", "tat-512.png"}) {
                    Config.copy(ctx.getAssets().open(f), new File(web, f));
                    Config.copy(ctx.getAssets().open(f), new File(keep, f));
                }
                String webVer = "0";
                try (java.io.InputStream in = ctx.getAssets().open("webver.txt")) {
                    webVer = new String(readAll(in), "UTF-8").trim();
                } catch (Exception ignored) { }
                if (!Python.isStarted()) Python.start(new AndroidPlatform(ctx));
                Python py = Python.getInstance();
                PyObject env = py.getModule("os").get("environ");
                env.callAttr("__setitem__", "KNX_BRIDGE_DATA", Config.dataDir(ctx).getAbsolutePath());
                env.callAttr("__setitem__", "KNX_BRIDGE_WEB", web.getAbsolutePath());
                env.callAttr("__setitem__", "TZ", java.util.TimeZone.getDefault().getID());
                env.callAttr("__setitem__", "KNX_APP_VERSION", BuildInfo.name(ctx) + " (" + Updater.localCode(ctx) + ")");
                env.callAttr("__setitem__", "KNX_APP_CODE", String.valueOf(Updater.localCode(ctx)));
                env.callAttr("__setitem__", "KNX_WEB_BUNDLED", webVer);
                // boot.py runs the newest online update of the page + Python (no reinstall), else the APK copy
                PyObject boot = py.getModule("boot");
                while (true) {
                    try {
                        boot.callAttr("run");
                        Thread.sleep(1000);  // returned for an online update: start the new copy
                        continue;
                    } catch (Throwable t) {
                        Log.e(TAG, "bridge stopped, restarting in 5 s", t);
                    }
                    Thread.sleep(5000);
                }
            } catch (Throwable t) {
                Log.e(TAG, "bridge failed to start", t);
                started = false;
            }
        }, "knx-bridge").start();
    }

    @Override public void onDestroy() {
        if (wake != null && wake.isHeld()) wake.release();
        if (wifi != null && wifi.isHeld()) wifi.release();
        if (multicast != null && multicast.isHeld()) multicast.release();
        super.onDestroy();
    }

    @Override public IBinder onBind(Intent intent) { return null; }

    private static byte[] readAll(java.io.InputStream in) throws java.io.IOException {
        java.io.ByteArrayOutputStream out = new java.io.ByteArrayOutputStream();
        byte[] buf = new byte[4096];
        int n;
        while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
        return out.toByteArray();
    }
}
