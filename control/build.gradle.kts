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
        versionCode = 4
        versionName = "2.2"
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}
