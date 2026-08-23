use std::collections::HashMap;
use std::hash::RandomState;
use anyhow::anyhow;
use indexmap::IndexMap;
use crate::backend::Backend;
use crate::elf::parser::ElfFile;
use crate::elf::symbol::ElfSymbol;
use crate::emulator::{AndroidEmulator, RcUnsafeCell, VMPointer};
use crate::linux::LinuxModule;
use crate::memory::svc_memory::HookListener;
use crate::tool;
use crate::tool::UnicornArg;

pub const WEAK_BASE: i64 = -1;
pub const BINDING_GLOBAL: i32 = 1;
pub const BINDING_WEAK: i32 = 2;
pub const BINDING_LOPROC: i32 = 13;
pub const BINDING_HIPROC: i32 = 15;


#[derive(Clone, Debug)]
pub struct ModuleSymbol {
    pub so_name: String,
    pub load_base: i64,
    pub symbol: Option<ElfSymbol>,
    pub relocation_addr: u64,
    pub to_so_name: String,
    pub offset: u64,
}

impl ModuleSymbol {
    pub fn new(
        so_name: String,
        load_base: i64,
        symbol: Option<ElfSymbol>,
        relocation_addr: u64,
        to_so_name: String,
        offset: u64
    ) -> Self {
        Self {
            so_name, load_base, offset, relocation_addr, symbol, to_so_name
        }
    }

    pub(crate) fn resolve<'a, T: Clone>(
        &self,
        emulator: &AndroidEmulator<'a, T>,
        modules: &IndexMap<String, RcUnsafeCell<LinuxModule<T>>>,
        resolve_weak: bool,
        listeners: &Vec<Box<dyn HookListener<'a, T>>>,
        cache_hook: &mut HashMap<u64, u64>,
        elf_file: &ElfFile
    ) -> anyhow::Result<ModuleSymbol> {
        let symbol_name = self.symbol.as_ref().unwrap().name(elf_file)?;
        for (lib_name, module_cell) in modules {
            let module = unsafe { &mut *module_cell.get() };
            let symbol = module.hook_map.get(&symbol_name);

            if let Some(symbol_hook) = symbol {
                return Ok(ModuleSymbol::new(
                    self.so_name.to_string(),
                    WEAK_BASE,
                    self.symbol.clone(),
                    self.relocation_addr,
                    module.name.clone(),
                    *symbol_hook
                ));
            }

            if module.dynsym.is_none() {
                //warn!("ModuleName: {}, dynsym is None, find: {}", module.name, symbol_name)
                continue
            }

            let elf_symbol = module.get_elf_symbol_by_name(&symbol_name);
            if let Ok(elf_symbol) = elf_symbol {
                if !elf_symbol.is_undefined() {
                    let binding = elf_symbol.binding();
                    if binding == BINDING_GLOBAL || binding == BINDING_WEAK {
                        let hash = tool::calculate_hash(&format!("{}#{}", module.name, symbol_name));
                        if cache_hook.contains_key(&hash) {
                            return Ok(ModuleSymbol::new(
                                self.so_name.to_string(),
                                WEAK_BASE,
                                self.symbol.clone(),
                                self.relocation_addr,
                                module.name.clone(),
                                *cache_hook.get(&hash).unwrap()
                            ));
                        }

                        for listener in listeners {
                            let hook = listener.hook(emulator, module.name.clone(), symbol_name.to_string(), module.base + elf_symbol.value as u64 + self.offset);
                            if hook > 0 {
                                cache_hook.insert(hash, hook);
                                module.hook_map.insert(symbol_name.to_string(), hook);
                                return Ok(ModuleSymbol::new(self.so_name.clone(), WEAK_BASE, Some(elf_symbol.clone()), self.relocation_addr, module.name.clone(), hook));
                            }
                        }

                        return Ok(ModuleSymbol::new(self.so_name.clone(), module.base as i64, Some(elf_symbol.clone()), self.relocation_addr, module.name.clone(), self.offset))
                    }
                }
            }
        }

        // Newer Bionic fortify and unlocked stdio entry points preserve the
        // ordinary fread/fwrite calling convention in their first four
        // arguments. Older Android runtimes can therefore service them with
        // the corresponding baseline implementation; the optional fifth
        // fortify size argument is safely ignored by AArch64 callees.
        let compatibility_alias = match symbol_name.as_str() {
            "__fread_chk" | "fread_unlocked" => Some("fread"),
            "__fwrite_chk" => Some("fwrite"),
            "__write_chk" => Some("write"),
            _ => None,
        };
        if let Some(alias_name) = compatibility_alias {
            for (_, module_cell) in modules {
                let module = unsafe { &*module_cell.get() };
                if let Ok(alias) = module.find_symbol_by_name(alias_name, false) {
                    return Ok(ModuleSymbol::new(
                        self.so_name.clone(),
                        WEAK_BASE,
                        self.symbol.clone(),
                        self.relocation_addr,
                        module.name.clone(),
                        alias.address() + self.offset,
                    ));
                }
            }
        }

        // Newer Android libraries may require lifecycle functions absent from
        // the older emulated libc image. Give the libc listener an opportunity
        // to provide those functions even when no exported symbol was found.
        if matches!(
            symbol_name.as_str(),
            "__cxa_atexit"
                | "__cxa_finalize"
                | "__register_atfork"
                | "_ZTH15ThreadedBufSize"
                | "__gcov_dump"
                | "__gcov_flush"
        ) {
            let hash = tool::calculate_hash(&format!("{}#{}", "libc.so", symbol_name));
            if let Some(hook) = cache_hook.get(&hash) {
                return Ok(ModuleSymbol::new(
                    self.so_name.clone(),
                    WEAK_BASE,
                    self.symbol.clone(),
                    self.relocation_addr,
                    "libc.so".to_string(),
                    *hook,
                ));
            }
            for listener in listeners {
                let hook = listener.hook(emulator, "libc.so".to_string(), symbol_name.clone(), self.offset);
                if hook > 0 {
                    cache_hook.insert(hash, hook);
                    return Ok(ModuleSymbol::new(
                        self.so_name.clone(),
                        WEAK_BASE,
                        self.symbol.clone(),
                        self.relocation_addr,
                        "libc.so".to_string(),
                        hook,
                    ));
                }
            }
        }

        if resolve_weak {
            if let Some(symbol) = &self.symbol {
                if symbol.binding() == BINDING_WEAK {
                    return Ok(ModuleSymbol::new(self.so_name.to_string(), WEAK_BASE, self.symbol.clone(), self.relocation_addr, "0".to_string(), 0));
                }
            }
        }

        match symbol_name.as_str() {
            "dlopen" | "dlclose" | "dlsym" | "dlerror" | "dladdr" |
            "android_update_LD_LIBRARY_PATH" | "android_get_LD_LIBRARY_PATH" | "dl_iterate_phdr" |
            "android_dlopen_ext" | "android_set_application_target_sdk_version" |
            "android_get_application_target_sdk_version" | "android_init_namespaces" |
            "android_create_namespace" | "dlvsym" | "android_dlwarning" |
            "dl_unwind_find_exidx" => {
                if resolve_weak {
                    let hash = tool::calculate_hash(&format!("{}#{}", "libdl.so", symbol_name));

                    if cache_hook.contains_key(&hash) {
                        return Ok(ModuleSymbol::new(
                            self.so_name.to_string(),
                            WEAK_BASE,
                            self.symbol.clone(),
                            self.relocation_addr,
                            "libdl.so".to_string(),
                            *cache_hook.get(&hash).unwrap()
                        ));
                    }

                    for listener in listeners {
                        let hook = listener.hook(&emulator, "libdl.so".to_string(), symbol_name.to_string(), self.offset);
                        if hook > 0 {
                            cache_hook.insert(hash, hook);
                            return Ok(ModuleSymbol::new(self.so_name.clone(), WEAK_BASE, self.symbol.clone(), self.relocation_addr, "libdl.so".to_string(), hook));
                        }
                    }
                }
                Err(anyhow!("Failed to resolve symbol: {}", symbol_name))
            }
            &_ => {
                Err(anyhow::Error::msg(format!("Failed to resolve symbol: {}", symbol_name)))
            }
        }
    }

    pub(crate) fn relocation<'a, T: Clone>(&self, owner: &mut LinuxModule<'a, T>, elf_file: &ElfFile, backend: &Backend<'a, T>) -> anyhow::Result<()> {
        if let Some(symbol) = self.symbol.as_ref() {
            let name = symbol.name(elf_file)?;
            owner.resolved_symbol.insert(name, self.clone());
        }
        let value = if self.load_base == WEAK_BASE {
            self.offset
        } else {
            self.load_base as u64 + self.symbol.as_ref().map(|s| s.value as u64).unwrap_or(0u64) + self.offset
        };
        let pointer = VMPointer::new(self.relocation_addr, 0, backend.clone());
        pointer.write_u64(value)?;
        Ok(())
    }
    
    pub(crate) fn relocation_ex2<'a, T: Clone>(&self, backend: &Backend<'a, T>, symbol: &ElfSymbol) -> anyhow::Result<()> {
        let value = if self.load_base == WEAK_BASE {
            self.offset
        } else {
            self.load_base as u64 + symbol.value as u64 + self.offset
        };
        let pointer = VMPointer::new(self.relocation_addr, 0, backend.clone());
        pointer.write_u64(value)?;
        Ok(())
    }

    pub(crate) fn relocation_ex<'a, T: Clone>(&self, resolved_symbol: &mut IndexMap<String, ModuleSymbol, RandomState>, elf_file: &ElfFile, backend: &Backend<'a, T>) -> anyhow::Result<()> {
        if let Some(symbol) = self.symbol.as_ref() {
            let name = symbol.name(elf_file)?;
            resolved_symbol.insert(name, self.clone());
        }
        let value = if self.load_base == WEAK_BASE {
            self.offset
        } else {
            self.load_base as u64 + self.symbol.as_ref().map(|s| s.value as u64).unwrap_or(0u64) + self.offset
        };
        let pointer = VMPointer::new(self.relocation_addr, 0, backend.clone());
        pointer.write_u64(value)?;
        Ok(())
    }
}

#[derive(Clone)]
pub struct LinuxSymbol {
    pub name: String,
    pub module_name: String,
    pub module_base: u64,
    pub symbol: ElfSymbol,
}

impl LinuxSymbol {
    pub fn new(name: String, symbol: ElfSymbol, base: u64, module_name: String) -> Self {
        Self {
            name,
            symbol,
            module_base: base,
            module_name,
        }
    }

    pub fn is_undefined(&self) -> bool {
        self.symbol.is_undefined()
    }

    pub fn module_name(&self) -> &str {
        &self.module_name
    }

    pub fn value(&self) -> u64 {
        self.symbol.value as u64
    }

    pub fn address(&self) -> u64 {
        self.module_base + self.value()
    }

    pub fn call<'a, T: Clone>(&self, emulator: &AndroidEmulator<'a, T>, args: Vec<UnicornArg>) -> Option<u64> {
        emulator.e_func(self.address(), args)
    }
}
