package com.secondbrain.vault

import android.content.Intent
import android.os.Bundle
import android.view.WindowManager
import com.getcapacitor.BridgeActivity

class MainActivity : BridgeActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        registerPlugin(SecondBrainHardwarePlugin::class.java)
        super.onCreate(savedInstanceState)
        // Prevent screenshots and recent-apps task switcher thumbnails from leaking decrypted vault notes
        window.setFlags(
            WindowManager.LayoutParams.FLAG_SECURE,
            WindowManager.LayoutParams.FLAG_SECURE
        )
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
    }
}
