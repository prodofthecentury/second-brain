import UIKit
import BackgroundTasks
import Capacitor

@UIApplicationMain
class AppDelegate: UIResponder, UIApplicationDelegate {

    var window: UIWindow?
    private var privacyShieldView: UIVisualEffectView?
    private let privacyShieldTag = 994201

    func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]?
    ) -> Bool {
        excludeApplicationSupportFromBackup()
        if #available(iOS 13.0, *) {
            BGTaskScheduler.shared.register(
                forTaskWithIdentifier: "com.secondbrain.vault.sync",
                using: nil
            ) { task in
                if let refreshTask = task as? BGAppRefreshTask {
                    refreshTask.expirationHandler = {
                        refreshTask.setTaskCompleted(success: false)
                    }
                    DispatchQueue.main.async {
                        NotificationCenter.default.post(
                            name: Notification.Name("SecondBrainBackgroundSync"),
                            object: nil
                        )
                        refreshTask.setTaskCompleted(success: true)
                    }
                } else {
                    task.setTaskCompleted(success: true)
                }
            }
        }
        return true
    }

    func applicationWillResignActive(_ application: UIApplication) {
        installPrivacyShield()
    }

    func applicationDidEnterBackground(_ application: UIApplication) {
        installPrivacyShield()
    }

    func applicationWillEnterForeground(_ application: UIApplication) {
        removePrivacyShield()
    }

    func applicationDidBecomeActive(_ application: UIApplication) {
        removePrivacyShield()
    }

    func applicationWillTerminate(_ application: UIApplication) {
        removePrivacyShield()
    }

    func application(
        _ app: UIApplication,
        open url: URL,
        options: [UIApplication.OpenURLOptionsKey: Any] = [:]
    ) -> Bool {
        return ApplicationDelegateProxy.shared.application(app, open: url, options: options)
    }

    func application(
        _ application: UIApplication,
        continue userActivity: NSUserActivity,
        restorationHandler: @escaping ([UIUserActivityRestoring]?) -> Void
    ) -> Bool {
        return ApplicationDelegateProxy.shared.application(
            application,
            continue: userActivity,
            restorationHandler: restorationHandler
        )
    }

    private func installPrivacyShield() {
        guard let targetWindow = activeKeyWindow() else { return }
        if targetWindow.viewWithTag(privacyShieldTag) != nil { return }
        let blurEffect = UIBlurEffect(style: .systemChromeMaterial)
        let shield = UIVisualEffectView(effect: blurEffect)
        shield.frame = targetWindow.bounds
        shield.autoresizingMask = [.flexibleWidth, .flexibleHeight]
        shield.tag = privacyShieldTag
        shield.isAccessibilityElement = false
        targetWindow.addSubview(shield)
        targetWindow.bringSubviewToFront(shield)
        privacyShieldView = shield
    }

    private func removePrivacyShield() {
        privacyShieldView?.removeFromSuperview()
        privacyShieldView = nil
        activeKeyWindow()?.viewWithTag(privacyShieldTag)?.removeFromSuperview()
    }

    private func activeKeyWindow() -> UIWindow? {
        if let win = self.window {
            return win
        }
        return UIApplication.shared.connectedScenes
            .compactMap { $0 as? UIWindowScene }
            .flatMap { $0.windows }
            .first { $0.isKeyWindow }
    }

    private func excludeApplicationSupportFromBackup() {
        guard let appSupport = FileManager.default.urls(
            for: .applicationSupportDirectory,
            in: .userDomainMask
        ).first else {
            return
        }
        var url = appSupport
        var values = URLResourceValues()
        values.isExcludedFromBackup = true
        try? url.setResourceValues(values)
    }
}
