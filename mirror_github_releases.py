import os
import json
import requests
from github import Github
from datetime import datetime
import subprocess

# 初始化配置
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
SOURCE_REPO = os.getenv("SOURCE_REPO")  # 源仓库（如"Snipaste/Snipaste"）
TARGET_REPO = os.getenv("GITHUB_REPOSITORY")  # 目标仓库（当前仓库）
RECORD_FILE = "synced_versions.json"  # 记录已同步版本的文件
BATCH_SIZE = 10  # 每同步10个版本保存一次


def load_synced_versions():
    """加载已同步的版本记录"""
    if not os.path.exists(RECORD_FILE):
        return []
    with open(RECORD_FILE, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return []  # 若文件损坏，视为无记录


def save_synced_versions(synced_list):
    """保存已同步的版本记录到JSON文件"""
    with open(RECORD_FILE, "w", encoding="utf-8") as f:
        json.dump(synced_list, f, indent=2, ensure_ascii=False)


def git_commit_and_push(message):
    """提交更新后的记录文件到仓库"""
    try:
        # 执行Git命令
        subprocess.run(["git", "config", "--global", "user.name", "github-actions[bot]"], check=True)
        subprocess.run(["git", "config", "--global", "user.email", "github-actions[bot]@users.noreply.github.com"], check=True)
        subprocess.run(["git", "add", RECORD_FILE], check=True)
        subprocess.run(["git", "commit", "-m", message], check=True)
        subprocess.run(["git", "push"], check=True)
        print(f"✅ 已提交记录：{message}")
    except subprocess.CalledProcessError as e:
        print(f"❌ Git提交失败：{str(e)}")
        raise  # 提交失败时中断执行，避免重复同步


def sync_release(release):
    """同步单个Release（创建Release并上传资产）"""
    g = Github(GITHUB_TOKEN)
    target_repo = g.get_repo(TARGET_REPO)
    
    # 检查目标仓库是否已存在该版本
    existing_tags = [t.name for t in target_repo.get_tags()]
    if release.tag_name in existing_tags:
        print(f"⚠️ 版本 {release.tag_name} 已存在，跳过同步")
        return False

    # 创建新Release
    new_release = target_repo.create_git_release(
        tag=release.tag_name,
        name=release.title,
        message=release.body,
        draft=release.draft,
        prerelease=release.prerelease
    )

    # 上传所有资产文件
    for asset in release.get_assets():
        print(f"📤 上传资产：{asset.name}（{asset.size} bytes）")
        asset.download_to_file(f"temp_{asset.name}")
        new_release.upload_asset(f"temp_{asset.name}", name=asset.name)
        os.remove(f"temp_{asset.name}")  # 清理临时文件

    print(f"✅ 同步完成：{release.tag_name}")
    return True


def main():
    g = Github(GITHUB_TOKEN)
    source_repo = g.get_repo(SOURCE_REPO)
    
    # 加载已同步记录
    synced_versions = load_synced_versions()
    print(f"📋 已同步版本数量：{len(synced_versions)}")

    # 获取源仓库的所有Release（按发布时间倒序）
    all_releases = list(source_repo.get_releases())
    all_releases.sort(key=lambda r: r.created_at, reverse=False)  # 按创建时间正序同步（从旧到新）

    # 筛选未同步的版本
    to_sync = [r for r in all_releases if r.tag_name not in synced_versions]
    print(f"📝 待同步版本数量：{len(to_sync)}")

    if not to_sync:
        print("✅ 所有版本已同步，退出程序")
        return

    # 开始同步（每BATCH_SIZE个版本保存一次）
    current_count = 0  # 当前批次同步数量
    for release in to_sync:
        try:
            # 同步单个版本
            if sync_release(release):
                synced_versions.append(release.tag_name)
                current_count += 1

                # 每同步10个版本，保存并提交记录
                if current_count >= BATCH_SIZE:
                    save_synced_versions(synced_versions)
                    git_commit_and_push(f"程序1：同步第 {len(synced_versions) - BATCH_SIZE + 1}-{len(synced_versions)} 个版本")
                    current_count = 0  # 重置计数器
        except Exception as e:
            print(f"❌ 同步 {release.tag_name} 失败：{str(e)}")
            # 保存已同步的记录后再退出
            save_synced_versions(synced_versions)
            git_commit_and_push(f"程序1：同步中断，已保存至 {len(synced_versions)} 个版本")
            raise  # 中断后续同步

    # 同步结束后，处理剩余不足10个的版本
    if current_count > 0:
        save_synced_versions(synced_versions)
        git_commit_and_push(f"程序1：同步完成，最终保存至 {len(synced_versions)} 个版本")

    print("🎉 所有待同步版本处理完成")


if __name__ == "__main__":
    main()
