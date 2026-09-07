$ErrorActionPreference = 'Stop'

$sourceDir = Join-Path $PSScriptRoot '..\data\knowledge\sources' | Resolve-Path -ErrorAction SilentlyContinue
if ($null -eq $sourceDir) {
    $sourceDir = Join-Path (Split-Path $PSScriptRoot -Parent) 'data\knowledge\sources'
}
$sourceDir = [System.IO.Path]::GetFullPath($sourceDir)
$manifestDir = [System.IO.Path]::GetFullPath((Join-Path (Split-Path $PSScriptRoot -Parent) 'data\knowledge\manifests'))

New-Item -ItemType Directory -Force -Path $sourceDir, $manifestDir | Out-Null

$sources = @(
    [pscustomobject]@{ key = 'government_procurement_law_2002_ndrc'; title = '中华人民共和国政府采购法'; url = 'https://www.ndrc.gov.cn/xxgk/zcfb/qt/200507/t20050706_967929.html'; role = '政府采购供应商资格、资格审查与采购人义务的法律背景' },
    [pscustomobject]@{ key = 'government_procurement_law_implementing_regulations_2015_mof'; title = '中华人民共和国政府采购法实施条例'; url = 'https://www.mof.gov.cn/zhengwuxinxi/zhengcefabu/201502/t20150227_1195516.htm'; role = '供应商资格材料、资格预审和利益冲突边界' },
    [pscustomobject]@{ key = 'procurement_goods_services_tender_order87_2017_mof'; title = '政府采购货物和服务招标投标管理办法'; url = 'https://www.mof.gov.cn/gp/xxgkml/tfs/201707/t20170718_2652766.htm'; role = '资格预审、资格性检查和招投标供应商审查流程' },
    [pscustomobject]@{ key = 'procurement_demand_management_2021_mof'; title = '政府采购需求管理办法'; url = 'http://fgk.mof.gov.cn/ui/src/views/law_html/85385.html'; role = '采购需求与供应商资格条件的合规边界' },
    [pscustomobject]@{ key = 'certification_accreditation_regulation_2023_moj'; title = '中华人民共和国认证认可条例'; url = 'https://xzfg.moj.gov.cn/front/law/detail?LawID=1688'; role = '认证证书、强制认证和认证真伪核验的法律边界' },
    [pscustomobject]@{ key = 'enterprise_information_publicity_regulation_2024_moj'; title = '企业信息公示暂行条例'; url = 'https://xzfg.moj.gov.cn/front/law/detail?LawID=1718'; role = '企业年报、公示信息和经营异常名录的资格风险依据' },
    [pscustomobject]@{ key = 'product_quality_law_cnipa'; title = '中华人民共和国产品质量法'; url = 'https://www.cnipa.gov.cn/art/2019/7/31/art_104_67810.html'; role = '产品质量与认证标志使用、伪造冒用的法律背景' },
    [pscustomobject]@{ key = 'compulsory_product_certification_rules_2025_isccc'; title = '强制性产品认证管理规定'; url = 'https://isccc.gov.cn/xxgk1/zcfg_3/bmgz/202507/t20250725_8567.htm'; role = '强制性产品认证目录、认证机构和证书管理' },
    [pscustomobject]@{ key = 'certification_certificate_mark_rules_2004_moj'; title = '认证证书和认证标志管理办法'; url = 'https://www.moj.gov.cn/pub/sfbgw/flfggz/flfggzbmgz/200409/t20040901_143677.html'; role = '认证证书和认证标志使用、监督与违规处理' },
    [pscustomobject]@{ key = 'supplier_audit_guideline_medical_device_2015_nmpa'; title = '医疗器械生产企业供应商审核指南'; url = 'https://www.nmpa.gov.cn/xxgk/ggtg/ylqxggtg/ylqxqtggtg/20150119120001125.html?type=pc&m='; role = '制造业供应商审核、评价和质量保证的官方指南' },
    [pscustomobject]@{ key = 'green_procurement_guideline_2014_mee'; title = '企业绿色采购指南'; url = 'https://www.mee.gov.cn/gkml/hbb/gwy/201412/t20141226_293493.htm'; role = '供应商绿色信息、采购合同和持续改进的参考指南' },
    [pscustomobject]@{ key = 'procurement_challenge_complaint_order94_2018_shenzhen'; title = '政府采购质疑和投诉办法'; url = 'https://www.sz.gov.cn/cn/xxgk/zfxxgj/zcfg/content/post_8964618.html'; role = '供应商异议、质疑和投诉的流程边界与负样本' }
)

$manifest = [System.Collections.Generic.List[object]]::new()
foreach ($item in $sources) {
    $path = Join-Path $sourceDir ($item.key + '.html')
    $status = $null
    $contentType = $null
    $bytes = $null
    $errorMessage = $null

    try {
        $response = Invoke-WebRequest -Uri $item.url -UseBasicParsing -TimeoutSec 60 -MaximumRedirection 5 -OutFile $path -PassThru
        $status = [int]$response.StatusCode
        $contentType = [string]$response.Headers['Content-Type']
        $bytes = (Get-Item -LiteralPath $path).Length
    }
    catch {
        $errorMessage = $_.Exception.Message
        if (Test-Path -LiteralPath $path) {
            Remove-Item -LiteralPath $path -Force
        }
    }

    $hash = $null
    if (Test-Path -LiteralPath $path) {
        $hash = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
    }

    $manifest.Add([pscustomobject]@{
        key = $item.key
        title = $item.title
        url = $item.url
        role = $item.role
        retrieved_at = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
        http_status = $status
        content_type = $contentType
        bytes = $bytes
        sha256 = $hash
        file = if ($hash) { 'sources/' + $item.key + '.html' } else { $null }
        error = $errorMessage
    })
}

$manifestPath = Join-Path $manifestDir 'source_catalog.json'
$manifest | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $manifestPath -Encoding UTF8
$manifest | Select-Object key, http_status, bytes, sha256, error | Format-Table -AutoSize
Write-Output "manifest=$manifestPath"
