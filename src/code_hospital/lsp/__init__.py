"""LSP 基座：内置薄客户端 + 语言注册表 SPI（docs/code-hospital-lsp-scope-draft.md）。

语言服务是外部进程（pyright / typescript-language-server / jdtls），医院不打包、
不 vendor；缺席时调用方回退文本兜底，行为与现状一致。
"""
