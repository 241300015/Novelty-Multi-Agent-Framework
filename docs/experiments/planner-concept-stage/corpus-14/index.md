# 14 篇论文查新点审阅索引

以下均为基于源 PDF 整理的候选点。页面为物理 PDF 页号；应用研究不据此认定架构创新。

| 论文 | 点数 | 类型 | 输入 |
| --- | ---: | --- | --- |
| Land Use and Land Cover Classification Using Deep Learning Techniques | 1 | application | [JSON](01-Land-Use/novelty-points.json) |
| Understanding and Improving Deep Neural Network for Activity Recognition | 1 | application | [JSON](02-Activity-Recognition/novelty-points.json) |
| Learning to Detect: A Data-driven Approach for Network Intrusion Detection | 1 | application | [JSON](03-Intrusion-Detection/novelty-points.json) |
| LightGCN: Simplifying and Powering Graph Convolution Network for Recommendation | 1 | method | [JSON](04-LightGCN/novelty-points.json) |
| N-BEATS: Neural Basis Expansion Analysis for Interpretable Time Series Forecasting | 1 | method | [JSON](05-N-BEATS/novelty-points.json) |
| MobileNetV2: Inverted Residuals and Linear Bottlenecks | 1 | method | [JSON](06-MobileNetV2/novelty-points.json) |
| Attention U-Net: Learning Where to Look for the Pancreas | 1 | method | [JSON](07-Attention-UNet/novelty-points.json) |
| Connecting the Dots Between Fact Verification and Fake News Detection | 1 | method | [JSON](08-Fake-News-Detection/novelty-points.json) |
| DDP-GCN: Multi-Graph Convolutional Network for Spatiotemporal Traffic Forecasting | 1 | method | [JSON](12-DDP-GCN/novelty-points.json) |
| Predicting Detection Filters for Small Footprint Open-vocabulary Keyword Spotting | 2 | method | [JSON](13-Keyword-Spotting/novelty-points.json) |
| TabNet: Attentive Interpretable Tabular Learning | 2 | method | [JSON](14-TabNet/novelty-points.json) |
| Music Transformer: Generating Music with Long-term Structure | 1 | method | [JSON](15-Music-Transformer/novelty-points.json) |
| 基于注意力网络的情感分析中的对比句处理 | 1 | method | [JSON](jsjyyyj.2022.02.0052.v2/novelty-points.json) |
| 基于深度学习的四川盆地丘陵区县域耕地遥感识别研究 | 1 | application | [JSON](基于深度学习的四川盆地丘陵区县域耕地遥感识别研究/novelty-points.json) |

## Land Use and Land Cover Classification Using Deep Learning Techniques

[输入](01-Land-Use/novelty-points.json) · [原文证据](01-Land-Use/evidence.json)

### NP-1

将图像识别预训练卷积神经网络迁移到高分辨率RGB正射遥感影像的土地利用与覆盖分类，并结合适合地理空间数据的数据增强。

Transfers image-recognition pretrained convolutional networks to land use and land cover classification of very-high-resolution RGB orthophotos, with geospatial data augmentation.

- 以可见光RGB高空间分辨率正射影像作为土地利用与覆盖分类输入。 / Uses very-high-resolution orthorectified visible-band RGB imagery for land use and land cover classification.
- 将ImageNet任务学到的深度网络迁移到遥感分类，以减少对大规模遥感标注数据的依赖。 / Transfers deep networks trained on ImageNet to remote sensing classification without requiring massive labeled remote sensing datasets.
- 加入适合地理空间数据的数据增强，并在未见过的影像数据上检验泛化。 / Applies geospatially suited dataset augmentation and evaluates generalization on unseen imagery.

源位置：assets/papers/01-Land-Use.pdf#page=2；assets/papers/01-Land-Use.pdf#page=3

## Understanding and Improving Deep Neural Network for Activity Recognition

[输入](02-Activity-Recognition/novelty-points.json) · [原文证据](02-Activity-Recognition/evidence.json)

### NP-1

通过可视化卷积神经网络提取的传感器活动特征，分析活动与特征的关系，并将显著特征送入DNN融合模型进行人体活动识别。

Visualizes sensor activity features extracted by a convolutional network, analyzes their relationship to activities, and feeds significant features to a DNN fusion model for human activity recognition.

- 在智能手机人体活动识别数据上使用卷积神经网络提取特征。 / Extracts features with a convolutional neural network on the Human Activity Recognition Using Smartphones dataset.
- 可视化所提取特征，分析神经网络依靠哪些特征识别活动。 / Visualizes extracted features to analyze how the network identifies activities.
- 将与活动相关的显著特征输入基于DNN的融合模型。 / Feeds significant activity-related features to a DNN-based fusion model.

源位置：assets/papers/02-Activity-Recognition.pdf#page=1

## Learning to Detect: A Data-driven Approach for Network Intrusion Detection

[输入](03-Intrusion-Detection/novelty-points.json) · [原文证据](03-Intrusion-Detection/evidence.json)

### NP-1

针对NSL-KDD流量采用先检测入侵、再识别攻击类别的分层策略，并在攻击分类阶段结合SVM-SMOTE过采样与深度神经网络处理类别不平衡。

Uses hierarchical detection on NSL-KDD traffic, first separating intrusions from normal behavior and then classifying attack types, with SVM-SMOTE oversampling and a deep neural network for imbalanced attack classes.

- 第一阶段进行正常流量与入侵流量的二分类。 / The first stage performs binary classification of normal traffic and intrusions.
- 第二阶段识别DoS、Probe、R2L与U2R四类攻击。 / The second stage classifies anomalies into DoS, Probe, R2L, and U2R.
- 在攻击分类中使用SVM-SMOTE过采样，并以深度神经网络作为分类模型。 / Uses SVM-SMOTE oversampling with a deep neural network for attack classification.

源位置：assets/papers/03-Intrusion-Detection.pdf#page=1；assets/papers/03-Intrusion-Detection.pdf#page=3

## LightGCN: Simplifying and Powering Graph Convolution Network for Recommendation

[输入](04-LightGCN/novelty-points.json) · [原文证据](04-LightGCN/evidence.json)

### NP-1

提出LightGCN，通过去除特征变换和非线性激活、保留邻域聚合，简化用于协同过滤的图卷积网络。

Proposes LightGCN, simplifying graph convolution for collaborative filtering by removing feature transformations and nonlinear activations while retaining neighborhood aggregation.

- 在用户与物品交互图上对嵌入进行线性传播。 / Linearly propagates user and item embeddings on the user-item interaction graph.
- 去掉特征变换和非线性激活，只保留邻域聚合。 / Removes feature transformation and nonlinear activation, retaining neighborhood aggregation.
- 对所有层学到的嵌入加权求和，形成最终嵌入。 / Forms final embeddings as weighted sums of embeddings learned at all layers.

源位置：assets/papers/04-LightGCN.pdf#page=1

## N-BEATS: Neural Basis Expansion Analysis for Interpretable Time Series Forecasting

[输入](05-N-BEATS/novelty-points.json) · [原文证据](05-N-BEATS/evidence.json)

### NP-1

提出N-BEATS，用全连接网络块和前向、后向双残差堆叠实现单变量时间序列点预测。

Proposes N-BEATS for univariate time series point forecasting using fully connected blocks and doubly residual stacking with forward and backward outputs.

- 每个全连接网络块产生预测与回溯重构两个输出。 / Each fully connected block produces a forecast and a backcast.
- 后续块接收前面块的残差输入，预测输出按层次聚合。 / Subsequent blocks receive residual inputs, while forecasts are aggregated hierarchically.
- 网络预测前向和后向基展开系数。 / The network predicts forward and backward basis expansion coefficients.

源位置：assets/papers/05-N-BEATS.pdf#page=1；assets/papers/05-N-BEATS.pdf#page=3

## MobileNetV2: Inverted Residuals and Linear Bottlenecks

[输入](06-MobileNetV2/novelty-points.json) · [原文证据](06-MobileNetV2/evidence.json)

### NP-1

提出MobileNetV2，将倒残差结构与线性瓶颈结合，在移动端模型中以较低计算开销保留特征表达能力。

Proposes MobileNetV2, combining inverted residuals and linear bottlenecks to retain representational power in mobile neural networks.

- 在较窄的瓶颈层之间建立快捷连接。 / Places shortcut connections between thin bottleneck layers.
- 中间扩展层使用轻量级逐通道卷积处理特征。 / Uses lightweight depthwise convolutions in the intermediate expansion layer.
- 移除窄层中的非线性激活以保持表达能力。 / Removes nonlinearities in narrow layers to maintain representational power.

源位置：assets/papers/06-MobileNetV2.pdf#page=1

## Attention U-Net: Learning Where to Look for the Pancreas

[输入](07-Attention-UNet/novelty-points.json) · [原文证据](07-Attention-UNet/evidence.json)

### NP-1

提出Attention U-Net，将可学习的注意力门加入U-Net，抑制无关区域并突出医学图像中的目标结构。

Proposes Attention U-Net, integrating learnable attention gates into U-Net to suppress irrelevant regions and highlight target structures in medical images.

- 注意力门学习抑制无关区域并突出任务相关特征。 / Attention gates learn to suppress irrelevant regions and highlight task-relevant features.
- 结合输入激活与来自更粗尺度的门控信号计算注意力系数。 / Computes attention coefficients from input activations and a gating signal from a coarser scale.
- 使用注意力系数缩放输入特征，以支持目标结构分割。 / Scales input features with attention coefficients for target structure segmentation.

源位置：assets/papers/07-Attention-UNet.pdf#page=1；assets/papers/07-Attention-UNet.pdf#page=4

## Connecting the Dots Between Fact Verification and Fake News Detection

[输入](08-Fake-News-Detection/novelty-points.json) · [原文证据](08-Fake-News-Detection/evidence.json)

### NP-1

通过新闻摘要和预训练事实核验模型的串联，将事实核验能力迁移到零样本虚假新闻检测。

Transfers fact verification to zero-shot fake news detection by composing news summarization with a pretrained fact verification model.

- 先用在新闻语料上预训练的摘要模型将长新闻压缩为短声明。 / First summarizes a long news article into a short claim with a model pretrained on news corpora.
- 再使用在FEVER数据上预训练的事实核验模型检测新闻真伪。 / Then uses a fact verification model pretrained on FEVER to detect whether the news is real or fake.
- 采用零样本方式，减少对虚假新闻任务训练数据的依赖。 / Enables zero-shot detection, reducing dependence on fake news training data.

源位置：assets/papers/08-Fake-News-Detection.pdf#page=1

## DDP-GCN: Multi-Graph Convolutional Network for Spatiotemporal Traffic Forecasting

[输入](12-DDP-GCN/novelty-points.json) · [原文证据](12-DDP-GCN/evidence.json)

### NP-1

提出DDP-GCN，将道路之间的距离、方向与位置关系共同纳入图卷积网络，建模交通速度预测中的空间依赖。

Proposes DDP-GCN, incorporating distance, direction, and positional relationships between roads into graph convolution for traffic speed forecasting.

- 除距离外，显式考虑交通方向与位置关系两类空间依赖。 / Explicitly considers direction and positional relationships in addition to distance.
- 将三种空间关系设计为基本图构件并纳入深度神经网络。 / Designs basic graph building blocks for the three spatial relationships and incorporates them into deep neural networks.

源位置：assets/papers/12-DDP-GCN.pdf#page=1

## Predicting Detection Filters for Small Footprint Open-vocabulary Keyword Spotting

[输入](13-Keyword-Spotting/novelty-points.json) · [原文证据](13-Keyword-Spotting/evidence.json)

### NP-1

提出开放词汇关键词检测方法，由辅助网络预测关键词检测层的参数，为新增关键词生成检测器而不要求任务专用训练数据。

Proposes open-vocabulary keyword spotting where an auxiliary network predicts keyword detection layer parameters to generate detectors for new keywords without task-specific training data.

- 辅助网络预测执行关键词检测的最上层参数。 / An auxiliary network predicts the topmost layer that performs keyword detection.
- 根据发音词典或字素到音素转换器得到关键词音素序列。 / Obtains keyword phone sequences from a pronunciation lexicon or a grapheme-to-phoneme converter.
- 关键词编码器计算卷积核，用生成的卷积核建立顶层检测层。 / A keyword encoder computes convolution kernels used to build the top detection layer.

源位置：assets/papers/13-Keyword-Spotting.pdf#page=1；assets/papers/13-Keyword-Spotting.pdf#page=2

### NP-2

在保留开放词汇关键词检测能力的同时，使用已有关键词的专用数据微调检测模型。

Fine-tunes the keyword spotting model with data for specific keywords while retaining its ability to handle new keywords.

- 当部分关键词具备专用训练数据时对模型进行微调。 / Fine-tunes the model when specific training data is available for some keywords.
- 微调后保留模型应用于新关键词的能力。 / Retains the ability to apply the model to new keywords after fine-tuning.

源位置：assets/papers/13-Keyword-Spotting.pdf#page=1

## TabNet: Attentive Interpretable Tabular Learning

[输入](14-TabNet/novelty-points.json) · [原文证据](14-TabNet/evidence.json)

### NP-1

提出TabNet，在各决策步骤利用序列注意力选择表格特征，并提供可解释的特征归因。

Proposes TabNet, using sequential attention to select tabular features at each decision step and provide interpretable feature attributions.

- 在每个决策步骤选择用于推理的特征。 / Selects features to reason from at each decision step.
- 使用稀疏特征掩码进行特征选择。 / Uses sparse feature masks for feature selection.
- 提供特征归因和模型全局行为的解释。 / Provides feature attributions and insights into global model behavior.

源位置：assets/papers/14-TabNet.pdf#page=1；assets/papers/14-TabNet.pdf#page=3

### NP-2

通过预测被掩蔽的表格特征对TabNet进行自监督预训练，利用未标注数据改善表格学习。

Uses self-supervised pretraining to predict masked tabular features in TabNet, exploiting unlabeled data for tabular learning.

- 通过预测被掩蔽的特征构造无监督预训练任务。 / Constructs an unsupervised pretraining task by predicting masked features.
- 在未标注数据丰富时使用自监督学习。 / Uses self-supervised learning when unlabeled data is abundant.

源位置：assets/papers/14-TabNet.pdf#page=1

## Music Transformer: Generating Music with Long-term Structure

[输入](15-Music-Transformer/novelty-points.json) · [原文证据](15-Music-Transformer/evidence.json)

### NP-1

提出高效相对注意力算法，将相对位置中间表示的内存需求从序列长度的二次量级降为线性量级，用于长序列音乐生成。

Proposes efficient relative attention that reduces the intermediate memory requirement for relative positional information from quadratic to linear in sequence length for long-form music generation.

- 以相对时间位置信息调节注意力。 / Modulates attention using relative timing information.
- 将相对位置信息中间表示的内存需求从序列长度的二次量级降低为线性量级。 / Reduces the memory requirement of intermediate relative positional information from quadratic to linear in sequence length.
- 将改进的相对注意力用于长音乐生成、给定动机续写与旋律条件伴奏。 / Applies the modified relative attention to long music generation, motif continuation, and melody-conditioned accompaniment.

源位置：assets/papers/15-Music-Transformer.pdf#page=1

## 基于注意力网络的情感分析中的对比句处理

[输入](jsjyyyj.2022.02.0052.v2/novelty-points.json) · [原文证据](jsjyyyj.2022.02.0052.v2/evidence.json)

### NP-1

提出结合BERT与相对位置自注意力网络的方面级情感分析模型，通过动态加权采样和标签平衡改善对比句及中性样本的识别。

Proposes aspect-level sentiment analysis combining BERT with relative-position self-attention, dynamic weighted sampling, and label balancing to improve contrastive-sentence and neutral-sample classification.

- 通过动态加权采样缓解对比句稀缺，使模型学习更多对比句特征。 / Uses dynamic weighted sampling to address scarce contrastive sentences and learn their features.
- 双头自注意力提取带相对位置的特征，并与预训练模型的绝对位置特征联合训练。 / Jointly trains relative-position features from double-head self-attention with absolute-position features from the pretrained model.
- 通过标签平衡对模型进行正则化，稳定对中性样本的辨识。 / Uses label balancing for regularization to stabilize neutral-sample identification.

源位置：assets/papers/jsjyyyj.2022.02.0052.v2.pdf#page=1

## 基于深度学习的四川盆地丘陵区县域耕地遥感识别研究

[输入](基于深度学习的四川盆地丘陵区县域耕地遥感识别研究/novelty-points.json) · [原文证据](基于深度学习的四川盆地丘陵区县域耕地遥感识别研究/evidence.json)

### NP-1

面向四川盆地丘陵县域耕地识别，将高分六号影像的光谱与地形特征融合后用于UNet++耕地提取，并对比仅使用光谱特征的方案。

For cultivated land identification in a hilly county of the Sichuan Basin, applies UNet++ to fused spectral and terrain features from GF-6 imagery and compares it with a spectral-only approach.

- 以高分六号高分辨率遥感影像开展三台县耕地识别。 / Uses high-resolution GF-6 imagery for cultivated land identification in Santai County.
- 比较UNet++、DeeplabV3+、UNet、PSPNet、随机森林与SinoLC-1产品的识别效果。 / Compares UNet++, DeeplabV3+, UNet, PSPNet, random forest, and the SinoLC-1 land cover product.
- 将光谱与地形特征融合输入UNet++，并与仅光谱输入进行对比。 / Fuses spectral and terrain features for UNet++ and compares this with spectral-only input.

源位置：assets/papers/基于深度学习的四川盆地丘陵区县域耕地遥感识别研究.pdf#page=1
