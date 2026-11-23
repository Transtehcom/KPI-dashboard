// Run against a fresh native server, using the same browser adapter delivered to Sites.
const assert = require("node:assert/strict");
const {webkit,devices} = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const base = process.argv[2] || "http://127.0.0.1:18765/";
const pages = ["home","kpi","funnel","funnel-fact","calls","gpt","workload","metrics-sales","metrics-supply","metrics-logistics","money","carriers","purchases","requests","suppliers","staff"];
const action = (page,key,value) => page.locator(`[data-act="${key}"]${value===undefined?"":`[data-val="${value}"]`}`).first();
async function navigate(page,name) {
    await page.goto(base+"#"+name);
    await page.locator(`#app[data-route="${name}"] main h1`).waitFor();
}
async function change(page, callback) {
    const rev=await page.locator("#app").getAttribute("data-revision");
    await callback();
    await page.waitForFunction(previous => {
        const next=document.querySelector("#app")?.dataset.revision;
        return next && next!==previous;
    },rev);
}
async function main() {
    const browser=await webkit.launch();
    const errors=[];
    try {
        for (const device of ["Desktop Safari","iPhone 13"]) {
            const ctx=await browser.newContext({...devices[device],acceptDownloads:true});
            const page=await ctx.newPage(); page.setDefaultTimeout(7000);
            page.on("pageerror",e=>errors.push(e.message));
            for(const name of pages) {
                await navigate(page,name);
                const within=await page.evaluate(()=>document.documentElement.scrollWidth<=document.documentElement.clientWidth+1);
                assert(within,device+" layout "+name);
            }
            if(device==="Desktop Safari"){
                await navigate(page,"home");
                await change(page,()=>action(page,"home-person").selectOption("logistics"));
                const closed=page.locator('button[data-act="home-work"][data-val="closed"]').first();
                const count=Number(await closed.locator("b").innerText());
                await change(page,()=>closed.click());
                assert.equal(await page.locator('[data-act="wl-open"]').count(),count);
                await navigate(page,"kpi");
                await change(page,()=>action(page,"kpi-person","logistics").click());
                await change(page,()=>action(page,"kpi-open").click());
                const review=page.locator('select[data-act^="kpi-review:"]').first();
                await change(page,()=>review.selectOption("yes"));
                assert.equal(await review.inputValue(),"yes");
                await navigate(page,"money");
                await change(page,()=>page.locator("button.stat").first().click());
                assert(await page.locator("#ops-detail").isVisible());
                await navigate(page,"carriers");
                const [download]=await Promise.all([page.waitForEvent("download"),page.getByRole("link",{name:"Скачать Excel"}).click()]);
                assert.equal(download.suggestedFilename(),"carriers.xlsx");
                await change(page,()=>action(page,"theme").click());
                const theme=await page.locator("html").getAttribute("data-theme");
                await page.reload();
                await page.locator("#app[data-revision]").waitFor();
                assert.equal(await page.locator("html").getAttribute("data-theme"),theme);
                await change(page,()=>action(page,"dialog","help").click());
                await page.locator('form[data-act="help-save"] textarea').fill("Учебное обращение из браузерной проверки");
                await change(page,()=>page.locator('form[data-act="help-save"] [type=submit]').click());
                assert(await page.getByRole("heading",{name:"Обращение сохранено"}).isVisible());
                await change(page,()=>page.keyboard.press("Escape"));
                const second=await ctx.newPage();
                await navigate(second,"home");
                await action(page,"theme").click();
                await page.getByRole("alert").filter({hasText:"другой вкладке"}).waitFor();
                await page.getByRole("button",{name:"Обновить представление"}).click();
                await page.getByRole("alert").waitFor({state:"hidden"});
                await change(page,()=>action(page,"session").selectOption("sales"));
                await page.goto(base+"#money");
                await page.waitForURL("**/#home");
                assert.equal(await action(page,"nav","money").count(),0);
                await second.close();
            }
            await ctx.close();
        }
        assert.deepEqual(errors,[]);
        console.log("PASS — all 16 views in desktop/mobile WebKit, drilldown, KPI decision, finance, XLSX, persistence, support, conflicts and role permissions.");
    } finally { await browser.close(); }
}
main().catch(error=>{console.error(error);process.exitCode=1;});
