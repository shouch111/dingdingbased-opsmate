# SolidWorks 上公司插件丢失

## 症状
- SolidWorks 中找不到公司自定义插件
- 插件工具栏或菜单消失

## 可能原因
- 插件对应文件夹被系统列入不可信名单，导致插件无法加载

## 解决步骤
1. 打开文件夹 `C:\Program Files\yonyou\CADIntegration\common\platform\plugin`
2. 右键点击该文件夹中的文件，选择「属性」
3. 在属性窗口底部，勾选「解除锁定」（如有安全提示）
4. 将该文件夹添加到系统的可信区间（Trusted Locations）
5. 赋予该文件夹下所有文件完整的读写操作权限
6. 重启 SolidWorks，检查插件是否恢复
