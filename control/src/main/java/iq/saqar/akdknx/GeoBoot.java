package iq.saqar.akdknx;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** After a restart or an update, put the home circle and the house alerts back. */
public class GeoBoot extends BroadcastReceiver {
    @Override public void onReceive(Context c, Intent intent) {
        String a = intent.getAction();
        if (Intent.ACTION_BOOT_COMPLETED.equals(a) || Intent.ACTION_MY_PACKAGE_REPLACED.equals(a)) {
            Geo.register(c);
            Push.start(c);
        }
    }
}
