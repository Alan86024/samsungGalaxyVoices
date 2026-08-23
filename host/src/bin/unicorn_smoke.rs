use unicorn_engine::{Arch, Mode, Permission, RegisterARM64, Unicorn};

#[no_mangle]
pub extern "C" fn getpagesize() -> i32 {
    4096
}

fn main() {
    let uc = Unicorn::new_with_data(Arch::ARM64, Mode::ARM, ()).expect("uc_open failed");
    let cloned = uc.clone();
    cloned.mem_map(0xfffe0000, 0x4000, Permission::READ | Permission::EXEC)
        .expect("high uc_mem_map failed");
    uc.mem_map(0x10000, 0x1000, Permission::ALL)
        .expect("uc_mem_map failed");
    uc.mem_write(0x10000, &[0x20, 0x00, 0x80, 0xd2])
        .expect("uc_mem_write failed");
    uc.emu_start(0x10000, 0x10004, 0, 0)
        .expect("uc_emu_start failed");
    let x0 = uc.reg_read(RegisterARM64::X0).expect("uc_reg_read failed");
    println!("x0={x0}");
    assert_eq!(x0, 1);
}
