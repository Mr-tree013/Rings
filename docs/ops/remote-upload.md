# 远端上传（GitHub）

## 现状

- 本仓库的发布方式是「本地完整开发 + 一次性推送到 GitHub」：提交、分支与 annotated tag 都先在
  本地完成，工作树 clean、验证命令全绿之后才推送。
- `origin` 走 SSH（`git@github.com:<OWNER>/<REPO>.git`）。如果当前 WSL 网络需要代理，通常只有
  HTTPS 通道需要；SSH 通道一般可以直接连通（`ssh -T git@github.com` 验证）。
- 若所在的网络封锁 22 端口（`ssh -T git@github.com` 报 `Connection closed`），GitHub 的
  **443 端口 SSH 通道**通常仍可用，直接把远端写成
  `ssh://git@ssh.github.com:443/<OWNER>/<REPO>.git` 即可（`ssh -T -p 443 git@ssh.github.com` 验证）。
- **已发布的 tag 永不改写**；README 之类的发布后文档整理属于 tag 之后的正常提交。

## 推送步骤

先在 GitHub 网页创建**空**仓库（不勾选 README / .gitignore / license，避免多出一个无关提交），
然后：

```bash
cd <PROJECT_DIR>

# 尚未配置时接上远端（SSH）
git remote add origin git@github.com:<OWNER>/<REPO>.git

# 推主分支与全部 tag；绝不使用 --force
git push -u origin main
git push origin --tags
```

验证：

```bash
git ls-remote --heads --tags origin | head
git log --oneline --decorate -5
git status
```

如果远端已经有与本机不一致的历史（别人的提交、不同的 tag），**停止**并报告，不要 force push。

## 推送前核对

- `git status` clean，且 `uv run ruff check .`、`uv run mypy src`、`uv run pytest` 全绿。
- 不含 `*.db` / `*.sqlite3` / `secrets/` / `state/` / `*.gab` 备份 / 个人 Vault /
  eHall 浏览器 profile；这些路径只存在于仓库之外的运行态目录。
- 文档与测试里没有真实凭据、真实账号标识或本机绝对个人路径（示例一律使用 placeholder）。

## 仓库外备份

不依赖网络的打包备份（单文件，可离线恢复）：

```bash
mkdir -p ~/backups
git bundle create ~/backups/<REPO>-$(date +%Y%m%d-%H%M).bundle --all --tags
git bundle verify ~/backups/<REPO>-<stamp>.bundle
```

从 bundle 恢复（在空目录里）：

```bash
git clone ~/backups/<REPO>-<stamp>.bundle <REPO>
```

注意：bundle 里只包含代码、文档、规则与测试，**不含**运行态数据库、知识索引与个人 Vault。
运行态的备份是另一回事，用 `pw backup create`（见 README 的 backup 章节）。
