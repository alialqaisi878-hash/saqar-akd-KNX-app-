plugins {
    id("com.android.application")
}

android {
    namespace = "iq.saqar.akdknx"
    compileSdk = 36
    defaultConfig {
        applicationId = "iq.saqar.akdknx"
        minSdk = 26
        targetSdk = 36
        versionCode = 15
        versionName = "3.4"
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
    signingConfigs {
        create("release") {
            val ks = System.getenv("TAT_KEYSTORE")
            if (ks != null && file(ks).exists()) {
                storeFile = file(ks)
                storePassword = System.getenv("TAT_KEYSTORE_PASS")
                keyAlias = "tat"
                keyPassword = System.getenv("TAT_KEYSTORE_PASS")
            }
        }
    }
    buildTypes {
        getByName("release") {
            isMinifyEnabled = false
            val ks = System.getenv("TAT_KEYSTORE")
            if (ks != null && file(ks).exists()) signingConfig = signingConfigs.getByName("release")
        }
    }
    buildFeatures { buildConfig = true }
    // direct = the APK we hand out (updates itself); play = Google Play / store build (the store updates it)
    flavorDimensions += "store"
    productFlavors {
        create("direct") { dimension = "store"; buildConfigField("String", "STORE", "\"direct\"") }
        create("play") { dimension = "store"; buildConfigField("String", "STORE", "\"play\"") }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}
