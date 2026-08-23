fn main() {
    println!("cargo:rustc-link-lib=c++");
    println!("cargo:rustc-link-lib=ole32");
    println!("cargo:rustc-link-lib=uuid");
}
