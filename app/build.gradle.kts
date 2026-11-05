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
        versionCode = 9
        versionName = "3.2"
        ndk { abiFilters += listOf("arm64-v8a", "x86_64") }
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
