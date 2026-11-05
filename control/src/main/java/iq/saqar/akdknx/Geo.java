package iq.saqar.akdknx;

import android.Manifest;
import android.app.PendingIntent;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.location.LocationManager;
import android.os.Build;
import android.util.Log;

import org.json.JSONObject;

import java.net.HttpURLConnection;
import java.net.URL;

/**
 * Arriving home / leaving home: Android tells us when the phone enters or leaves the circle around the house,
 * and we call the house link so the bridge can run the owner's scene. Works without Google services.
 */
public class Geo extends BroadcastReceiver {
    static final String PREF = "akd";

    static SharedPreferences prefs(Context c) { return c.getSharedPreferences(PREF, Context.MODE_PRIVATE); }

    static boolean hasPermission(Context c) {
        return c.checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED;
    }

    private static PendingIntent pending(Context c) {
        Intent i = new Intent(c, Geo.class).setAction("iq.saqar.akdknx.GEO");
        int fl = PendingIntent.FLAG_UPDATE_CURRENT | (Build.VERSION.SDK_INT >= 31 ? PendingIntent.FLAG_MUTABLE : 0);
        return PendingIntent.getBroadcast(c, 41, i, fl);
    }

    /** (Re)registers the circle from the saved settings. Returns false when location permission is missing. */
    static boolean register(Context c) {
        String cfg = prefs(c).getString("geo", "");
        if (cfg.isEmpty() || !hasPermission(c)) return false;
        try {
            JSONObject o = new JSONObject(cfg);
            LocationManager lm = c.getSystemService(LocationManager.class);
            PendingIntent pi = pending(c);
            lm.removeProximityAlert(pi);
            lm.addProximityAlert(o.getDouble("lat"), o.getDouble("lng"), (float) o.optDouble("radius", 150), -1, pi);
            return true;
        } catch (SecurityException | org.json.JSONException e) {
            Log.w("Geo", "register", e);
            return false;
        }
    }

    static void disable(Context c) {
        prefs(c).edit().remove("geo").apply();
        try { c.getSystemService(LocationManager.class).removeProximityAlert(pending(c)); } catch (Exception ignored) { }
    }

    @Override public void onReceive(Context c, Intent intent) {
        if (!intent.hasExtra(LocationManager.KEY_PROXIMITY_ENTERING)) return;
        boolean entering = intent.getBooleanExtra(LocationManager.KEY_PROXIMITY_ENTERING, false);
        SharedPreferences p = prefs(c);
        String last = p.getString("geo_last", "");
        String now = entering ? "arrive" : "leave";
        if (now.equals(last)) return; // Android can repeat the same event
        p.edit().putString("geo_last", now).apply();
        String cfg = p.getString("geo", "");
        if (cfg.isEmpty()) return;
        final PendingResult res = goAsync();
        new Thread(() -> {
            try {
                String url = new JSONObject(cfg).getString(now);
                for (int i = 0; i < 3; i++) {   // the phone may only just have got mobile data back
                    try {
                        HttpURLConnection h = (HttpURLConnection) new URL(url).openConnection();
                        h.setRequestMethod("POST");
                        h.setConnectTimeout(10000);
                        h.setReadTimeout(15000);
                        h.setDoOutput(true);
                        h.getOutputStream().close();
                        if (h.getResponseCode() < 500) break;
                    } catch (Exception e) {
                        Thread.sleep(8000);
                    }
                }
            } catch (Exception e) {
                Log.w("Geo", "send", e);
            } finally {
                res.finish();
            }
        }).start();
    }
}
