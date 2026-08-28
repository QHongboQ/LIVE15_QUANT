# DEP-SERVICE-RESTART-001 — Upstream WinSW v2.12.0 findings

Scope: primary upstream documentation, source, and issue reports for WinSW
v2.12.0. This note records no local-runtime observations or conclusions.

## Immutable version scope

WinSW v2.12.0 resolves to upstream commit
[`eef5bade59fca0254e387ac73ed7625ba6aa7147`][winsw-v212].

## Configuration discovery

- WinSW v2.12.0 obtains the currently running wrapper executable path, derives
  its directory and basename, and then loads only a same-basename `.xml` or
  `.yml` file in that directory. If neither exists, it raises a
  `FileNotFoundException`.[^program-config]
- The v2 installation guide therefore directs operators to rename the wrapper
  to (for example) `myapp.exe`, create `myapp.xml`, and place the two files
  side-by-side because that is how WinSW discovers its configuration.[^install]
- Issue [#1015][issue-1015] documents this v2.12.0 limitation for a differently
  named configuration file. Issue [#1118][issue-1118] independently reports
  that v2.12.0 ignores an arbitrarily specified configuration path in its
  legacy/global-mode interface.

## `%BASE%` and paths

WinSW v2.12.0 expands `%Name%` environment-variable references in XML. It sets
`BASE` itself to the directory containing the renamed WinSW executable, and the
child process can also access that value.[^xml-base]

## Restart and configuration lifetime

- In v2.12.0, the `restart` command stops the SCM service, waits for
  `Stopped`, starts it, and waits for `Running`.[^program-restart]
- The configuration is loaded before command dispatch/service execution and is
  held by `WrapperService` for that wrapper process.[^program-start][^wrapper]
  The documented v2 command list has no `refresh` or `reload` command.[^commands]
- WinSW’s v2 documentation further states that SCM offers no atomic restart;
  its restart behavior is stop followed by start.[^restart-doc]

## Out-of-scope issue

Issue [#1285][issue-1285] concerns v3 alpha `autoRefresh` behavior with a
restricted `<serviceaccount>`. It is not evidence of v2.12.0 behavior.

[^program-config]: [WinSW v2.12.0 `Program.cs`, configuration discovery][program-config].
[^install]: [WinSW v2.12.0 installation guide][install].
[^xml-base]: [WinSW v2.12.0 XML configuration documentation][xml-base].
[^program-restart]: [WinSW v2.12.0 `Program.cs`, `Restart`][program-restart].
[^program-start]: [WinSW v2.12.0 `Program.cs`, service-mode startup][program-start].
[^wrapper]: [WinSW v2.12.0 `WrapperService`, configuration ownership][wrapper].
[^commands]: [WinSW v2.12.0 `Program.cs`, supported commands][commands].
[^restart-doc]: [WinSW v2.12.0 self-restart documentation][restart-doc].

[winsw-v212]: https://github.com/winsw/winsw/tree/eef5bade59fca0254e387ac73ed7625ba6aa7147
[program-config]: https://github.com/winsw/winsw/blob/eef5bade59fca0254e387ac73ed7625ba6aa7147/src/WinSW/Program.cs#L641-L647
[install]: https://github.com/winsw/winsw/blob/eef5bade59fca0254e387ac73ed7625ba6aa7147/doc/installation.md#L5-L11
[xml-base]: https://github.com/winsw/winsw/blob/eef5bade59fca0254e387ac73ed7625ba6aa7147/doc/xmlConfigFile.md#L23-L30
[program-restart]: https://github.com/winsw/winsw/blob/eef5bade59fca0254e387ac73ed7625ba6aa7147/src/WinSW/Program.cs#L414-L481
[program-start]: https://github.com/winsw/winsw/blob/eef5bade59fca0254e387ac73ed7625ba6aa7147/src/WinSW/Program.cs#L81-L99
[wrapper]: https://github.com/winsw/winsw/blob/eef5bade59fca0254e387ac73ed7625ba6aa7147/src/WinSW/WrapperService.cs#L22-L24
[commands]: https://github.com/winsw/winsw/blob/eef5bade59fca0254e387ac73ed7625ba6aa7147/src/WinSW/Program.cs#L166-L221
[restart-doc]: https://github.com/winsw/winsw/blob/eef5bade59fca0254e387ac73ed7625ba6aa7147/doc/selfRestartingService.md#L9-L11
[issue-1015]: https://github.com/winsw/winsw/issues/1015
[issue-1118]: https://github.com/winsw/winsw/issues/1118
[issue-1285]: https://github.com/winsw/winsw/issues/1285
