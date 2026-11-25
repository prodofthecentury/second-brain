# Keep Capacitor bridge and plugin contracts for R8/ProGuard release builds
-keep class com.getcapacitor.** { *; }
-keep class com.secondbrain.vault.** { *; }
-keepclasseswithmembers class * {
    @com.getcapacitor.PluginMethod <methods>;
}
-keep @com.getcapacitor.annotation.CapacitorPlugin class * { *; }
-keepattributes SourceFile,LineNumberTable
