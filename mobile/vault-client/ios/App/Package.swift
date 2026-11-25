// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "SecondBrainVault",
    platforms: [.iOS(.v13)],
    products: [
        .library(
            name: "SecondBrainVault",
            targets: ["SecondBrainVault"]
        )
    ],
    dependencies: [
        .package(url: "https://github.com/ionic-team/capacitor-swift-pm.git", from: "6.0.0")
    ],
    targets: [
        .target(
            name: "SecondBrainVault",
            dependencies: [
                .product(name: "Capacitor", package: "capacitor-swift-pm"),
                .product(name: "Cordova", package: "capacitor-swift-pm")
            ],
            path: "App"
        )
    ]
)
