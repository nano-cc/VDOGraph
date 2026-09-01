# 实体消歧报告
## 统计概览
- **总实体数（抽取）**: 139
- **标准实体数（消歧后）**: 87
- **合并的实体数**: 52
- **合并率**: 37.4%
- **Level 1 命中（精确匹配）**: 40
- **Level 2 命中（模糊匹配）**: 2
- **Level 3 命中（LLM 判断）**: 10
- **新实体数**: 87

---

## 合并的实体（24 个）
这些实体在多个片段中出现，已被合并：

### 中国人 (Person)
- **来源片段数**: 3
- **来源片段**: media_1_segment_0, media_1_segment_5, media_1_segment_7
- **描述**: 中国人民，指中国公民群体，在文中讨论牛肉价格对其影响

### 全国牛肉批发价 (Concept)
- **来源片段数**: 3
- **来源片段**: media_1_segment_0, media_1_segment_1, media_1_segment_2
- **别名**: 牛肉价格
- **描述**: 指中国国内牛肉的批发市场价格，文中提到2024年6月达到33元/斤

### 电商平台 (Organization)
- **来源片段数**: 2
- **来源片段**: media_1_segment_0, media_1_segment_10
- **描述**: 指在线购物平台，文中提及2023-2024年进口牛肉在此以低价销售

### 进口牛肉 (Product)
- **来源片段数**: 8
- **来源片段**: media_1_segment_0, media_1_segment_2, media_1_segment_3, media_1_segment_4, media_1_segment_7, media_1_segment_9, media_1_segment_10, media_1_segment_11
- **别名**: 巴西冷冻去骨牛肉, 进口冻品牛肉, 进口冻肉
- **描述**: 指从国外进口的牛肉产品，文中描述其价格曾低至十几元一斤

### 社交媒体 (Organization)
- **来源片段数**: 2
- **来源片段**: media_1_segment_0, media_1_segment_11
- **描述**: 指网络社交平台，文中提到用户曾分享低价牛排信息

### 牛肉自由 (Concept)
- **来源片段数**: 2
- **来源片段**: media_1_segment_0, media_1_segment_9
- **描述**: 指消费者能轻松负担牛肉消费的状态，文中描述曾短暂流行

### 进口牛肉配额制 (Concept)
- **来源片段数**: 2
- **来源片段**: media_1_segment_0, media_1_segment_1
- **别名**: 配额
- **描述**: 指2024年1月1日起实施的政策，对进口牛肉实行配额管理

### 澳大利亚 (Location)
- **来源片段数**: 5
- **来源片段**: media_1_segment_0, media_1_segment_1, media_1_segment_8, media_1_segment_10, media_1_segment_11
- **别名**: 澳洲
- **描述**: 指主要牛肉进口国之一，文中提到上半年已用完全年配额

### 巴西 (Location)
- **来源片段数**: 4
- **来源片段**: media_1_segment_1, media_1_segment_7, media_1_segment_8, media_1_segment_11
- **描述**: Brazil, a country mentioned in the context of beef import quotas and consumption

### 中国 (Location)
- **来源片段数**: 7
- **来源片段**: media_1_segment_1, media_1_segment_2, media_1_segment_4, media_1_segment_6, media_1_segment_7, media_1_segment_8, media_1_segment_11
- **描述**: China, the country discussed in relation to beef prices, import restrictions, and domestic cattle farming

### 牛肉 (Product)
- **来源片段数**: 5
- **来源片段**: media_1_segment_1, media_1_segment_4, media_1_segment_5, media_1_segment_7, media_1_segment_9
- **别名**: 国内牛肉
- **描述**: Beef, the primary product discussed, involving import tariffs, quotas, prices, and domestic production

### 关税 (Concept)
- **来源片段数**: 3
- **来源片段**: media_1_segment_1, media_1_segment_4, media_1_segment_10
- **描述**: Tariffs, specifically mentioned as a 55% increase on Australian beef imports starting June 20

### 肉牛养殖产业 (Organization)
- **来源片段数**: 2
- **来源片段**: media_1_segment_1, media_1_segment_3
- **别名**: 国内养牛产业
- **描述**: The domestic cattle farming industry in China, described in terms of profit changes and losses

### 养殖户 (Person)
- **来源片段数**: 5
- **来源片段**: media_1_segment_1, media_1_segment_3, media_1_segment_4, media_1_segment_8, media_1_segment_9
- **别名**: 养牛户, 农户
- **描述**: Cattle farmers in China, particularly those experiencing losses in the beef industry

### 配额政策 (Concept)
- **来源片段数**: 2
- **来源片段**: media_1_segment_4, media_1_segment_9
- **描述**: 配额政策 is the quota policy implemented in 2025 to regulate beef imports, maintaining a low-tax quota of 2.68 million tons to balance domestic industry protection and consumer access to affordable beef.

### 养猪 (Concept)
- **来源片段数**: 2
- **来源片段**: media_1_segment_5, media_1_segment_6
- **描述**: Pig farming, described as a large-scale industrial activity in China, capable of high output in facilities like multi-story pig farms.

### 养鸡 (Concept)
- **来源片段数**: 2
- **来源片段**: media_1_segment_5, media_1_segment_6
- **描述**: Chicken farming, also industrialized in China, with facilities capable of producing millions of chickens annually.

### 养牛 (Concept)
- **来源片段数**: 2
- **来源片段**: media_1_segment_5, media_1_segment_6
- **描述**: Cattle farming, noted as difficult to industrialize in China due to long breeding cycles and low reproduction rates.

### 养猪的上市公司 (Organization)
- **来源片段数**: 3
- **来源片段**: media_1_segment_5, media_1_segment_5, media_1_segment_5
- **别名**: 养鸡的上市公司, 养牛的上市公司
- **描述**: Publicly listed companies involved in pig farming, mentioned as existing in China.

### 东北 (Location)
- **来源片段数**: 3
- **来源片段**: media_1_segment_6, media_1_segment_8, media_1_segment_9
- **描述**: 东北 refers to Northeast China, another region with grassland used for initial cattle rearing in the "北牛奶运" strategy.

### 阿根廷 (Location)
- **来源片段数**: 3
- **来源片段**: media_1_segment_7, media_1_segment_8, media_1_segment_11
- **描述**: A country in South America, highlighted for its Pampas grasslands and efficient cattle grazing conditions.

### 美国 (Location)
- **来源片段数**: 2
- **来源片段**: media_1_segment_7, media_1_segment_8
- **描述**: A country in North America, mentioned for its natural cattle grazing methods and larger cattle size compared to China.

### 养牛产业 (Concept)
- **来源片段数**: 2
- **来源片段**: media_1_segment_8, media_1_segment_11
- **描述**: The cattle raising industry in China is discussed in terms of high costs and competition with foreign countries like Brazil, Argentina, the US, and Australia

### 日韩 (Location)
- **来源片段数**: 2
- **来源片段**: media_1_segment_10, media_1_segment_11
- **描述**: Refers to Japan and South Korea, used as comparative examples in the text for beef import policies and costs.


## 疑似应该合并但未合并的实体
以下实体可能应该合并，但由于相似度低于阈值未合并：

- **国家** vs **中国**
  - 国家: 1 个片段
  - 中国: 7 个片段

## 所有标准实体（87 个）
| 序号 | 实体名称 | 类型 | 来源片段数 | 别名 |
|------|---------|------|-----------|------|
| 1 | 中国人 | Person | 3 | - |
| 2 | 全国牛肉批发价 | Concept | 3 | 牛肉价格 |
| 3 | 菜市场 | Location | 1 | - |
| 4 | 电商平台 | Organization | 2 | - |
| 5 | 进口牛肉 | Product | 8 | 巴西冷冻去骨牛肉, 进口冻品牛肉, 进口冻肉 |
| 6 | 社交媒体 | Organization | 2 | - |
| 7 | 牛肉自由 | Concept | 2 | - |
| 8 | 猪肉价格 | Concept | 1 | - |
| 9 | 进口牛肉配额制 | Concept | 2 | 配额 |
| 10 | 国家 | Organization | 1 | - |
| 11 | 澳大利亚 | Location | 5 | 澳洲 |
| 12 | 巴西 | Location | 4 | - |
| 13 | 中国 | Location | 7 | - |
| 14 | 牛肉 | Product | 5 | 国内牛肉 |
| 15 | 关税 | Concept | 3 | - |
| 16 | 肉牛养殖产业 | Organization | 2 | 国内养牛产业 |
| 17 | 养殖户 | Person | 5 | 养牛户, 农户 |
| 18 | 种牛堵牛环节 | Concept | 1 | - |
| 19 | 疯牛病 | Event | 1 | - |
| 20 | 走私牛肉 | Concept | 1 | - |
| 21 | 国内牛肉产量 | Concept | 1 | - |
| 22 | 非洲猪瘟 | Event | 1 | - |
| 23 | 中国牛存栏量 | Concept | 1 | - |
| 24 | 商务部 | Organization | 1 | - |
| 25 | 配额政策 | Concept | 2 | - |
| 26 | 老百姓 | Person | 1 | - |
| 27 | 农产品工业化 | Concept | 1 | - |
| 28 | 养牛大省 | Location | 1 | - |
| 29 | 全国肉类总产量 | Concept | 1 | - |
| 30 | 猪肉 | Product | 1 | - |
| 31 | 禽肉 | Product | 1 | - |
| 32 | 养猪 | Concept | 2 | - |
| 33 | 养鸡 | Concept | 2 | - |
| 34 | 养牛 | Concept | 2 | - |
| 35 | 26层的养猪场 | Location | 1 | - |
| 36 | 建筑面积12万平方米的养鸡场 | Location | 1 | - |
| 37 | 养猪的上市公司 | Organization | 3 | 养鸡的上市公司, 养牛的上市公司 |
| 38 | 养扇贝的上市公司 | Organization | 1 | - |
| 39 | 母牛 | Product | 1 | - |
| 40 | 架子牛 | Product | 1 | - |
| 41 | 育肥厂 | Location | 1 | - |
| 42 | 肉牛 | Product | 1 | - |
| 43 | 母猪 | Product | 1 | - |
| 44 | 小猪 | Product | 1 | - |
| 45 | 北牛奶运 | Concept | 1 | - |
| 46 | 内蒙 | Location | 1 | - |
| 47 | 东北 | Location | 3 | - |
| 48 | 山东 | Location | 1 | - |
| 49 | 河北 | Location | 1 | - |
| 50 | 安徽 | Location | 1 | - |
| 51 | 阿根廷 | Location | 3 | - |
| 52 | 美国 | Location | 2 | - |
| 53 | 新疆 | Location | 1 | - |
| 54 | 锡林格勒 | Location | 1 | - |
| 55 | 潘帕斯大草原 | Location | 1 | - |
| 56 | 豆粕 | Product | 1 | - |
| 57 | 玉米 | Product | 1 | - |
| 58 | 资源禀赋 | Concept | 1 | - |
| 59 | 天然放牧 | Concept | 1 | - |
| 60 | 精料育肥 | Concept | 1 | - |
| 61 | 降成本 | Concept | 1 | - |
| 62 | 山西忻州 | Location | 1 | - |
| 63 | 西门塔尔牛 | Product | 1 | - |
| 64 | 养牛产业 | Concept | 2 | - |
| 65 | 成本 | Concept | 1 | - |
| 66 | 散户 | Concept | 1 | - |
| 67 | 大户 | Concept | 1 | - |
| 68 | 冻肉 | Product | 1 | - |
| 69 | 自助餐厅 | Location | 1 | - |
| 70 | 火锅店 | Location | 1 | - |
| 71 | 连锁餐 | Organization | 1 | - |
| 72 | 潮汕火锅店 | Organization | 1 | - |
| 73 | 南美 | Location | 1 | - |
| 74 | 鲜切牛肉 | Product | 1 | - |
| 75 | 国产牛肉 | Product | 1 | - |
| 76 | 进口配额 | Concept | 1 | - |
| 77 | 养牛成本 | Concept | 1 | - |
| 78 | 日本 | Location | 1 | - |
| 79 | 日韩 | Location | 2 | - |
| 80 | 韩国 | Location | 1 | - |
| 81 | 和牛 | Product | 1 | - |
| 82 | 韩牛 | Product | 1 | - |
| 83 | 韩国人吃不起牛肉 | Concept | 1 | - |
| 84 | 戴老板 | Person | 1 | - |
| 85 | 牛肉进口关税 | Concept | 1 | - |
| 86 | 高端化 | Concept | 1 | - |
| 87 | 本期视频 | Event | 1 | - |
