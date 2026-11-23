package ru.transtehcom.kpi;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;

public final class MainActivity extends Activity {
    private static final Uri DASHBOARD = Uri.parse("https://kpi.transtehcom.ru:8443/");

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        openDashboard();
    }

    private void openDashboard() {
        Intent view = new Intent(Intent.ACTION_VIEW, DASHBOARD);
        view.addCategory(Intent.CATEGORY_BROWSABLE);

        // A null session requests a Custom Tab without binding a browser service.
        // Other browsers can handle the same HTTPS intent as an ordinary tab.
        Bundle options = new Bundle();
        options.putBinder("android.support.customtabs.extra.SESSION", null);
        options.putInt("android.support.customtabs.extra.TOOLBAR_COLOR", 0xff3a4185);
        view.putExtras(options);

        try {
            startActivity(view);
            finish();
        } catch (ActivityNotFoundException unavailable) {
            new AlertDialog.Builder(this)
                .setTitle("Не удалось открыть дашборд")
                .setMessage("Установите или включите браузер и повторите попытку.")
                .setPositiveButton("Повторить", (dialog, which) -> openDashboard())
                .setNegativeButton("Закрыть", (dialog, which) -> finish())
                .setOnCancelListener(dialog -> finish())
                .show();
        }
    }
}
