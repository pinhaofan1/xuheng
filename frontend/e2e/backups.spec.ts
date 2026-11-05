import { test, expect } from '@playwright/test'
import { readFile } from 'node:fs/promises'

test('paired backup can be created, downloaded and checked without restoring live data',async({page})=>{
  const errors:string[]=[]
  page.on('pageerror',e=>errors.push(e.message))
  await page.goto('/book/ai-browser-fixture/operations')
  await page.getByRole('button',{name:'数据备份',exact:true}).click()
  await expect(page.getByText('数据备份与恢复',{exact:true})).toBeVisible()
  await page.getByRole('button',{name:'创建联合备份',exact:true}).click()
  await expect(page.getByText('校验通过',{exact:true})).toBeVisible({timeout:15000})
  const row=page.locator('.backup-row').first()
  await expect(row).toContainText('3 章')
  const downloadPromise=page.waitForEvent('download')
  await row.getByRole('link',{name:'下载',exact:true}).click()
  const download=await downloadPromise
  expect(download.suggestedFilename()).toMatch(/^xuheng-[0-9a-f]{32}\.zip$/)
  const bytes=await readFile((await download.path())!)
  await page.getByLabel('选择备份文件',{exact:true}).setInputFiles({name:'fixture.zip',mimeType:'application/zip',buffer:bytes})
  await expect(page.getByText('校验通过',{exact:true})).toBeVisible({timeout:15000})
  await page.setViewportSize({width:390,height:844})
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true)
  const panel=page.locator('.n-drawer-body-content-wrapper').last()
  expect(await panel.evaluate(el=>el.scrollWidth<=el.clientWidth)).toBe(true)
  await page.screenshot({path:'../.validation/backups-mobile.png',fullPage:false,animations:'disabled'})
  await page.getByLabel('选择备份文件',{exact:true}).setInputFiles({name:'broken.zip',mimeType:'application/zip',buffer:Buffer.from('damaged')})
  await expect(page.getByText('操作未完成',{exact:true})).toBeVisible()
  await expect(page.getByText('校验通过',{exact:true})).toHaveCount(0)
  await expect(page.getByText('restore-xuheng.cmd',{exact:true})).toBeVisible()
  expect(errors).toEqual([])
})
