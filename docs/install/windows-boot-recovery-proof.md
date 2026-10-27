# Windows reboot proof for legacy host recovery

The installer may retire only the exact host markers captured in its immutable,
private preparation checkpoint. It holds the install and Runtime bootstrap locks,
uses Runtime's task interruption fence, and captures markers under the generic
host lifecycle lock. Preparation requires native proof that the recorded leader
is absent. A missing PID alone never authorizes marker retirement.

Resume requires a fresh boot identity from the same machine and a strictly
increased boot sequence. The host helper copies exact marker bytes into private
quarantine before removing them. A partial interruption can resume only with the
same checkpoint and matching quarantined bytes. A changed or newly published
registration is refused. The helper never signals a PID, writes a Runtime Job
receipt, or changes a retained backup. A later process reusing the old numeric
PID cannot be the process from the preboot checkpoint, so that collision does
not authorize any action on the new process and does not require another reboot.

Ordinary same-boot host retirement still requires a retained Windows Job handle
or the exact authorized Runtime Job-drain receipt. Optional `boot_identity` state
metadata does not bypass those guards. An unavailable boot query does not prevent
normal host startup; it prevents using reboot proof.

## Boot source and its limits

Windows identity is `{machine_guid, boot_id}` under the scheme
`windows-kernel-boot-id-v1`. MachineGuid comes from the 64-bit HKLM Cryptography
view. Reads request `KEY_READ`, never registry write access.

Microsoft documents `KUSER_SHARED_DATA.BootId` as the OS loader's boot sequence.
The implementation reads the x64 shared page through `ReadProcessMemory` on its
own process. A failed or short read rejects the proof. It checks the kernel's
version, build, architecture and positive sequence before using the value.
See [KUSER_SHARED_DATA](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/ntddk/ns-ntddk-kuser_shared_data)
and [ReadProcessMemory](https://learn.microsoft.com/en-us/windows/win32/api/memoryapi/nf-memoryapi-readprocessmemory).

The kernel sequence must agree with HKLM's `PrefetchParameters\\BootId`, which
Microsoft describes as the successful-boot counter. Kernel reads bracket the
registry reads. A stale or late registry update cannot turn a same-boot sequence
into permission to retire markers: disagreement blocks capture. See
[Memory integrity enablement](https://learn.microsoft.com/en-us/windows-hardware/design/device-experiences/oem-hvci-enablement).

The user shared-page mapping at `0x7ffe0000` and field offsets are an ABI
dependency, **not an exported Win32 boot-identity API**. The bounded prefix starts
at `0x260`; its architecture/version fields precede BootId at `0x2c4`. Only known
Windows 10/11 x64 build families are accepted. Other architectures, unknown
builds, missing registry values, type errors, access denial and counter
disagreement fail closed. Adding a build requires native qualification.

Comparison requires the same MachineGuid and a strictly larger positive DWORD
sequence. Equal sequences mean the same boot; decreases, resets, wraparound and
machine changes are refused. Administrative edits to OS identity, OS image
restoration and altered kernel mappings are outside this proof's trust boundary.
There is no time, uptime, PID-absence, event-log or BCD-GUID fallback.

`NtQuerySystemInformation(SystemBootEnvironmentInformation=0x5a).BootIdentifier`
identifies the boot-loader entry and is deliberately rejected as a recovery
scheme. Shape checks and same-boot stability would not establish that such a
GUID changes after Restart. Microsoft also cautions that this native API can
change: [NtQuerySystemInformation](https://learn.microsoft.com/en-us/windows/win32/api/winternl/nf-winternl-ntquerysysteminformation).

## Qualification evidence

Hermetic tests cover the ABI size/offset checks, partial/denied reads, typed
read-only registry access, kernel/registry mismatch, counter equality,
decrease/reset/wrap, machine changes, invalid identities, exact marker bindings,
private files, new registrations, interruption replay and unchanged normal
same-boot cleanup requirements.

The `windows_native` test in `tests/core/execution/test_boot_identity.py` queries
the actual kernel/registry source and requires equality in a second Python
process. On Windows, unsupported or unavailable proof is a test failure, not a
skip. Passing that test establishes API availability and same-boot consistency;
it does not establish the full restart journey.

Before advertising a qualified recovery journey for a Windows release, run on
a disposable native VM: prepare against exact legacy markers, verify same-boot
resume is refused, use Windows **Restart**, collect both source identities, and
resume the same checkpoint. Require a larger sequence, the same machine GUID,
exact quarantine contents, an unchanged backup and a verified candidate service.
Exercise a new postboot registration and require refusal. Repeat on Windows 10
and the supported Windows 11 build, including Unicode paths and the ordinary
non-administrator user. No real Windows reboot was performed during the local
macOS implementation run.
