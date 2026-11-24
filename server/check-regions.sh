#!/usr/bin/env bash
# 在 Azure Cloud Shell (Bash) 里运行：扫一遍候选区域，列出"本订阅真正可用"的 B 系列 VM 大小。
# 用法：bash check-regions.sh
set -u
REGIONS="eastus2 westus2 southeastasia eastasia japaneast centralus eastus westus northcentralus"
echo "=============================================================="
echo " 扫描本订阅在各区域的可用 B/F/A 系列规格（只看不受限的）"
echo "=============================================================="
for r in $REGIONS; do
  line=$(az vm list-skus -l "$r" --resource-type virtualMachines \
    --query "[?starts_with(name,'Standard_B') && length(restrictions)==\`0\`].name | sort(@) | [0:6]" -o tsv 2>/dev/null)
  if [ -n "$line" ]; then
    echo
    echo "✅ $r  可用:"
    echo "$line" | awk '{printf "   - %s\n", $0}'
  else
    echo "❌ $r  无可用 B 系列（或该区域对本订阅关闭）"
  fi
done
echo
echo "=============================================================="
echo " 建议：挑上面第一个出现的区域，然后用 az vm create 建机"
echo "=============================================================="
