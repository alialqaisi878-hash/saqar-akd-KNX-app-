plugins {
    id("com.android.application")
}

android {
    namespace = "iq.saqar.akdknx"
    compileSdk = 34
    defaultConfig {
        applicationId = "iq.saqar.akdknx"
        minSdk = 26
        targetSdk = 34
        versionCode = 5
        versionName = "2.3"
    }
    // fixed company key from the CI secret, so updates install over the old app (falls back to the debug key)
    signingConfigs {
        getByName("debug") {
            val ks = System.getenv("TAT_KEYSTORE")
            if (ks != null && file(ks).exists()) {
                storeFile = file(ks)
                storePassword = System.getenv("TAT_KEYSTORE_PASS")
                keyAlias = "tat"
                keyPassword = System.getenv("TAT_KEYSTORE_PASS")
            }
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}
