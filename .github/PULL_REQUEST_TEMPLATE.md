## 改了什么

<!-- 行为变化与原因；关联 issue 写 Closes #N -->

## 怎么验证的

- [ ] `COLLECTOR_ENABLED=0 python -m unittest discover -s chanapp/tests`
- [ ] 改了 `chanapp/web/` 时：`bash chanapp/scripts/test_js.sh`
- [ ] 不含凭据、本机路径与个人信息；新增 provider 单测用录制 fixture，不访问外网
