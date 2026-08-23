pub mod android;
pub mod emulator;
pub mod keystone;
pub mod linux;
pub mod memory;
pub mod pointer;
pub(crate) mod tool;
pub(crate) mod elf;
mod backend;

pub use backend::RegisterARM64;
pub use emulator::AndroidEmulator;
pub use tool::UnicornArg;
