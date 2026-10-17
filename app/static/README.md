# 控制台样式

`console.css` 是随项目提供的 Tailwind CSS 3.4.17 产物（MIT），登录页与控制台共用。运行和部署无需 Node.js，也不会向外部 CDN 请求样式、字体或图表脚本。

开发时若修改了 `app/main.py` 中的 Tailwind 类名，在仓库根目录重新生成并提交 CSS：

```bash
npx --yes tailwindcss@3.4.17 --content ./app/main.py --output ./app/static/console.css --minify
```

布局和组件的定制样式保留在 `app/main.py` 的 `<style>` 中；请求比例图使用原生 CSS。生成器仅扫描本项目源码。
