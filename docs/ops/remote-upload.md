# 远端上传（延后一次性推送）

## 现状

- 本仓库目前**没有配置任何 git remote**，也从未推送过。
- 所有提交、分支与 annotated tag 都完整保存在本机（`main` 工作树干净）。
- 原因：开启 GitHub 需要代理，而代理与当前 WSL 网络配置冲突。因此按阶段在本地提交，
  等做到一个合适的节点再一次性上传。
- 顺带实测：GitHub 的 **SSH** 通道从当前 WSL 可以直接连通（`ssh -T git@github.com` 成功认证为
  `Mr-tree013`，`git ls-remote` 能拿到真实服务端响应），所以到推送时通常**不需要**为 SSH 配代理；
  需要代理的通常是 HTTPS 通道。

## 每个阶段的要求（本地）

1. 一个阶段 = 一个提交（Conventional Commits），完成后 `git status` 必须 clean。
2. 阶段收尾时打 annotated tag，并写明该阶段交付内容；**已发布的 tag 永不改写**。
3. 短生命周期分支保留在本地（不删除），便于回溯每个阶段的演进。
4. 提交前核对：不得包含 `*.db` / `*.sqlite3` / `secrets/` / `state/` / 个人 Vault。
5. 定期做仓库外备份（见下）。

## 一次性上传的步骤

先在 GitHub 网页创建**空**仓库（建议 Private，名称 `growing-assistant`，
不勾选 README/.gitignore/license，避免多出一个无关提交），然后：

```bash
cd /home/mrtree/projects/growing-assistant

# 1) 接上远端（SSH）
git remote add origin git@github.com:Mr-tree013/growing-assistant.git

# 2) 推主分支与全部 tag
git push -u origin main
git push origin --tags

# 3) 可选：把每个阶段的 feature 分支也推上去（保留演进轨迹）
git push origin feat/event-store feat/event-inbox feat/durable-event-worker \
                feat/storage-catalog feat/knowledge-index feat/index-sync-service
```

验证：

```bash
git ls-remote --heads --tags origin | head
git log --oneline --decorate -5
```

## 仓库外备份

不依赖网络的打包备份（单文件，可离线恢复）：

```bash
mkdir -p /home/mrtree/backups
git bundle create /home/mrtree/backups/growing-assistant-$(date +%Y%m%d-%H%M).bundle --all --tags
git bundle verify /home/mrtree/backups/growing-assistant-<stamp>.bundle
```

从 bundle 恢复（在空目录里）：

```bash
git clone /home/mrtree/backups/growing-assistant-<stamp>.bundle growing-assistant
```

注意：bundle 里只包含代码、文档、规则与测试，**不含**运行态数据库、知识索引与个人 Vault。

