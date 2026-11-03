package iq.saqar.knx;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** Starts the bridge after the tablet restarts (power cut) or after an app update. */
public class BootReceiver extends BroadcastReceiver {
    @Override public void onReceive(Context context, Intent intent) {
        BridgeService.start(context);
    }
}
