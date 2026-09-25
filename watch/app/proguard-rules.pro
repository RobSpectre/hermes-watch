# kotlinx.serialization keeps its generated serializers in the companion objects;
# shrinking without these rules produces runtime NoSuchMethodError on the watch.
-keepattributes *Annotation*, InnerClasses
-dontnote kotlinx.serialization.**
-keepclassmembers class com.nousresearch.hermeswatch.data.** {
    *** Companion;
}
-keepclasseswithmembers class com.nousresearch.hermeswatch.data.** {
    kotlinx.serialization.KSerializer serializer(...);
}
-keep,includedescriptorclasses class com.nousresearch.hermeswatch.data.**$$serializer { *; }

# OkHttp ships optional platform integrations that reference absent classes.
-dontwarn okhttp3.internal.platform.**
-dontwarn org.conscrypt.**
-dontwarn org.bouncycastle.**
-dontwarn org.openjsse.**
