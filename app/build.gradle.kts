plugins {
    id("com.android.application")
    id("com.chaquo.python")
}

android {
    namespace = "iq.saqar.knx"
    compileSdk = 34
    defaultConfig {
        applicationId = "iq.saqar.knx"
        minSdk = 26
        targetSdk = 34
        versionCode = 22
        versionName = "5.0"
        ndk { abiFilters += listOf("arm64-v8a", "x86_64") }
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

chaquopy {
    defaultConfig {
        version = "3.12"
        pip {
            // pure-python builds (NO_EXTENSIONS env vars are set by the CI workflow)
            install("-r", "requirements-android.txt")
        }
    }
}
