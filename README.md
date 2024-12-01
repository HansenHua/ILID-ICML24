# ILID
This work "How to Leverage Diverse Demonstrations in Offline Imitation Learning" has been accepted by ICML'24.
## :page_facing_up: Description
we introduce a simple yet effective data selection method that identifies positive behaviors based on their \emph{resultant states} -- a more informative criterion enabling explicit utilization of dynamics information and effective extraction of both expert and beneficial diverse behaviors. Further, we devise a lightweight behavior cloning algorithm capable of leveraging the expert and selected data correctly. In the experiments, we evaluate our method on a suite of complex and high-dimensional offline IL benchmarks, including continuous-control and vision-based tasks. The results demonstrate that our method achieves state-of-the-art performance, outperforming existing methods on \textbf{20/21} benchmarks, typically by \textbf{2-5x}, while maintaining a comparable runtime to Behavior Cloning (\texttt{BC}).
## :wrench: Dependencies
- Python == 3.7 (Recommend to use [Anaconda](https://www.anaconda.com/download/#linux) or [Miniconda](https://docs.conda.io/en/latest/miniconda.html))
- [PyTorch == 1.8.1](https://pytorch.org/)
- [MuJoCo == 2.3.6](http://www.mujoco.org) 
- NVIDIA GPU (RTX A6000) + [CUDA 11.1](https://developer.nvidia.com/cuda-downloads)
### Installation
1. Clone repo
    ```bash
    git clone [https://github.com/HansenHua/ILID-ICML24.git](https://github.com/HansenHua/ILID-ICML24.git)
    cd ILID-offline-imitation-learning
    ```
2. Install dependent packages
    ```
    pip install -r requirement.txt
    ```
## :zap: Quick Inference

Get the usage information of the project
```bash
cd code
python main.py -h
```

## :computer: Training

We provide complete training codes for ILIDE.<br>
You could adapt it to your own needs.

	```
    python main.py
	```
	The log files will be stored in [https://github.com/HansenHua/ILID-offline-imitation-learning](https://github.com/HansenHua/ILID-offline-imitation-learning).
## :checkered_flag: Testing
Illustration

We alse provide the performance of our model. The illustration videos are stored in [ILID-ICML24/performance](https://github.com/HansenHua/ILID-ICML24/tree/main/performance).

## :e-mail: Contact

If you have any question, please email `huaxy24@mails.tsinghua.edu.cn`.
