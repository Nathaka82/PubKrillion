# Local answer search

The server loads the quantised ONNX models and their WordPiece tokenizers from this directory. Each model is distributed under the Apache 2.0 licence of its upstream project. [Licence text](LICENSE.txt)

| Files | Original model | Pinned ONNX release | SHA-256 of model_quantized.onnx |
| --- | --- | --- | --- |
| Root model and tokenizer | [all-MiniLM-L6-v2](https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2) | [Xenova conversion](https://huggingface.co/Xenova/all-MiniLM-L6-v2/tree/751bff37182d3f1213fa05d7196b954e230abad9) | `afdb6f1a0e45b715d0bb9b11772f032c399babd23bfc31fed1c170afc848bdb1` |
| Model and tokenizer in `reranker/` | [MS MARCO MiniLM L6 v2](https://huggingface.co/cross-encoder/ms-marco-MiniLM-L6-v2) | [Xenova conversion](https://huggingface.co/Xenova/ms-marco-MiniLM-L-6-v2/tree/a09144355adeed5f58c8ed011d209bf8ee5a1fec) | `e9d8ebf845c413e981c175bfe49a3bfa9b3dcce2a3ba54875ee5df5a58639fbe` |

`npm run build:answer-index` generates `catalogue.sqlite` from the shipped questions, model and WordNet dictionary. The index contains answer text, dictionary definitions and embeddings. Game sessions and team guesses are stored separately. [WordNet licence](../WordNet-LICENSE.txt)
