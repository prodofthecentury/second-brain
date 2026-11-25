import Foundation
import UIKit
import LocalAuthentication
import Security
import BackgroundTasks
import UserNotifications
import Capacitor

@objc(SecondBrainHardwarePlugin)
public class SecondBrainHardwarePlugin: CAPPlugin, CAPBridgedPlugin {
    public let identifier = "SecondBrainHardwarePlugin"
    public let jsName = "SecondBrainHardware"
    public let pluginMethods: [CAPPluginMethod] = [
        CAPPluginMethod(name: "unlockWithBiometrics", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "storeHardwareSecret", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "loadHardwareSecret", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "scheduleBackgroundSync", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "postSyncNotification", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "getHardwareStatus", returnType: CAPPluginReturnPromise)
    ]

    private let keychainService = "com.secondbrain.vault.keystore"
    private let bgTaskIdentifier = "com.secondbrain.vault.sync"

    @objc func getHardwareStatus(_ call: CAPPluginCall) {
        let ctx = LAContext()
        var err: NSError?
        let canBio = ctx.canEvaluatePolicy(.deviceOwnerAuthenticationWithBiometrics, error: &err)
        call.resolve([
            "platform": "ios",
            "biometricsAvailable": canBio,
            "secureEnclaveKeystore": true,
            "backgroundSyncTaskId": bgTaskIdentifier
        ])
    }

    @objc func unlockWithBiometrics(_ call: CAPPluginCall) {
        let reason = call.getString("reason") ?? "Unlock your encrypted Second Brain Vault"
        let ctx = LAContext()
        var err: NSError?
        guard ctx.canEvaluatePolicy(.deviceOwnerAuthenticationWithBiometrics, error: &err) else {
            call.reject(err?.localizedDescription ?? "Biometric authentication unavailable on this device.")
            return
        }
        ctx.evaluatePolicy(.deviceOwnerAuthenticationWithBiometrics, localizedReason: reason) { success, evalErr in
            DispatchQueue.main.async {
                if success {
                    call.resolve(["authenticated": true, "method": "faceid_touchid"])
                } else {
                    call.reject(evalErr?.localizedDescription ?? "Biometric authentication canceled or failed.")
                }
            }
        }
    }

    @objc func storeHardwareSecret(_ call: CAPPluginCall) {
        guard let account = call.getString("key"), let value = call.getString("value"),
              let data = value.data(using: .utf8) else {
            call.reject("Missing key or value for iOS Keychain storage.")
            return
        }
        let accessControl = SecAccessControlCreateWithFlags(
            nil,
            kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
            .biometryCurrentSet,
            nil
        )
        let delQuery: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: keychainService,
            kSecAttrAccount as String: account
        ]
        SecItemDelete(delQuery as CFDictionary)

        var addQuery: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: keychainService,
            kSecAttrAccount as String: account,
            kSecValueData as String: data
        ]
        if let ac = accessControl {
            addQuery[kSecAttrAccessControl as String] = ac
        }
        let status = SecItemAdd(addQuery as CFDictionary, nil)
        if status == errSecSuccess {
            call.resolve(["stored": true, "account": account])
        } else {
            call.reject("Keychain SecItemAdd failed with OSStatus \(status)")
        }
    }

    @objc func loadHardwareSecret(_ call: CAPPluginCall) {
        guard let account = call.getString("key") else {
            call.reject("Missing key for iOS Keychain lookup.")
            return
        }
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: keychainService,
            kSecAttrAccount as String: account,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne
        ]
        var result: AnyObject?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        if status == errSecSuccess, let data = result as? Data, let secret = String(data: data, encoding: .utf8) {
            call.resolve(["found": true, "value": secret])
        } else if status == errSecItemNotFound {
            call.resolve(["found": false, "value": NSNull()])
        } else {
            call.reject("Keychain SecItemCopyMatching failed with OSStatus \(status)")
        }
    }

    @objc func scheduleBackgroundSync(_ call: CAPPluginCall) {
        let intervalMins = max(15, call.getInt("intervalMinutes") ?? 60)
        if #available(iOS 13.0, *) {
            let request = BGAppRefreshTaskRequest(identifier: bgTaskIdentifier)
            request.earliestBeginDate = Date(timeIntervalSinceNow: TimeInterval(intervalMins * 60))
            do {
                try BGTaskScheduler.shared.submit(request)
                call.resolve(["scheduled": true, "taskId": bgTaskIdentifier, "intervalMinutes": intervalMins])
            } catch {
                call.resolve(["scheduled": false, "taskId": bgTaskIdentifier, "reason": error.localizedDescription])
            }
        } else {
            call.resolve(["scheduled": false, "reason": "requires_ios_13"])
        }
    }

    @objc func postSyncNotification(_ call: CAPPluginCall) {
        let title = call.getString("title") ?? "Second Brain Vault"
        let body = call.getString("body") ?? "Vault sync completed."
        let center = UNUserNotificationCenter.current()
        center.requestAuthorization(options: [.alert, .sound]) { granted, _ in
            guard granted else {
                call.resolve(["sent": false, "reason": "permission_denied"])
                return
            }
            let content = UNMutableNotificationContent()
            content.title = title
            content.body = body
            content.sound = .default
            let req = UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil)
            center.add(req) { err in
                if let e = err {
                    call.resolve(["sent": false, "reason": e.localizedDescription])
                } else {
                    call.resolve(["sent": true])
                }
            }
        }
    }
}
