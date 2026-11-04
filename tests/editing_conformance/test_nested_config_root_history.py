"""Exact accepted config-root redo history authenticates before overlap guards."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import zlib

import pytest

from astrid.sdk.timeline._collections import AcceptedRecords
from astrid.sdk.timeline._command_state import CommandError, State
from astrid.sdk.timeline._commit_patch import inverse_address_resolver
from astrid.sdk.timeline._doc_patch import inverse_changes
from astrid.sdk.timeline._step_undo import (
    check_later_steps,
    inverse_stages,
    select_inverse_records,
    validate_nested_history,
)
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import (
    bundle_from_document,
    create_runtime_commit_service,
    document_from_bundle,
)
from tests.sdk.test_timeline_commit_provider import CommitRejected, DetachedPort, DetachedRealm
from tests.sdk.test_timeline_commit_provider import runtime_abi as runtime_abi

# Lossless accepted history, closures and failing request from qualified packet
# 2041, SHA256 8bf50adeb0b21c0d3e621d890dadd5d26221c415d048c12505f69be4a3dbb13d.
_CAPTURE = (
    'c-ri}*>WRCk~aD(GTaWT6#K&a{?ClrKQoe!WOLD0MvJwG(<DIwWJxtLzWedWOn?B1B>^N7i(G9jRUv_l&HeK=-2WPc@#Ay)^LcRd'
    '*I*RW!_)XVJ%;bM|4QEnH-pjRw=|iiPm`2irfD!NAB=;09*rN%5BxZOFaLE<qucVu>SH<_csEVAPcQy{RDbt&6yashe?EQ^GAqi7'
    'kBUg4V{(pDmqhR?$Y5L?G(VoCZ=>n_m+s~HZaf)1e*N_1Ch76{)9mplD~J=;8sdc|oGF(Tv5x61SQJ(VuCt58FdnGQIT@EslF?)q'
    'oN_5@7%z+;9^7M`4sQMlE17(qmK7~bzC5Vr9<mlx1jR#OLRjUE$|9<ybV$}yGWha39E_jJN=Fare)O0=H~R;pOv7Dza7(*1P4_9l'
    '#y`H?-wy}#Cj&Zo{W2WH$@kmI3;aF1`)L{u?wwEf@btIGIR0Tazz->op5g5eY4SC}!#qm&u#(~6X)^x*;N{Ks*<^W0S|pwX3Dzm2'
    'R3eIV8Y~y8OmF-$38}37+sMIBp1#A5KMr6))93V5?@hB&x8q41J;J2mLjn`6hIm`(2*~LXRy7!Gne&r-4tMYlc*V0TyZ+BFW!<x{'
    'H8eA*_B2fznyV%;dj7TfQvDiu%izav^v7n=(;v4j_{Z5G(;uIItlwD1sSbU5mnQJOGRg4--akq1{-K@>^_G!DOwK+CpPc8CYp0p#'
    'Kw>WE>f67+r0Fv<1v15vnWCYz+dDTc^iK^*R2phM(Nb#zEAq~YY+^EsT1SmDmQc;Jk~wP5ykIt(U^S6SXH#Ao$KmAx=2;!~dh#L+'
    '2!+_-xp6vr#aIm7z+MLGc#6dPoT+r&Y9pC|!L$_Aq|AXFQ;4j*J*KDh7%NEw+c(M$&n(QKY(d$!3GC|aoL}qDA7Hm!{m1Fuxc>I*'
    'im%T(rukjHlKH1)#7g|ZXL0nknghJhF{y4ORl&zVlx9ggAZn%pV?_FFlxK!euDOlW2rd=;&lsM96wx!-am%f&&hq>H7`V5LazA=%'
    '<^Tic(bsn$K2KcuS4~jW2|ta(^UI`A&c8;F<pY@Ti@OINDBKjn%LEn@X4^4IUq{pDNfE`b6=k%8iG$Dabq|(W!SQf{?+<DPy{}&f'
    'GeE#RQW(ajOa&(r;EkvNxCd;JIg@~dIZ-bRJR*wd3<pMN@REz{4y4ERG*q@ySmU^(j(X}1ODcI|g#?rg0Jtg}qnY!D!_KDU8TB&2'
    'e1oPkS~^&Nma_@G?E*+8;Q)+1jOCOFQMxQWu>y|GYonr2%D~AfICKLeWH=}W^b0!|6FeHM*!2ZgTfa&H;)6=fGEuO_#^fji2oabC'
    'AuzlYoF(86DkV)S!<Z@AK!`UXa1c&p&<s+K+SN;6hwEo-0+U)85ax<!YXJ|lmdb|&gdr4AzoeEYS@w__t`i(Z;*1b@c^1@#l`}s0'
    'rJH69|B{pkok&)o9vI>{hj%0(Zv$FMV!*C85#q9uz-%UK@0k=#D?yk}dgY7{e(CE1{;r=pz$FuwgD${;E|oQ^U}nrACkvvADyt05'
    '4$xDABw`@DayCRM4J06`41-O;-V0x>8?3DTZ6Hv9WO7M+O+#?n@v?3MTc~A*|D}Ra$|(la3QX>3$id3!Gtm)rK2@~53JR-1^637d'
    'M*Gb|?Wo|hLV$r@FrB6{%r`d~Ie=jiGbfrcLqQ9c(pe9T>jl-I6oOvCWfIY(C?x^?gL6=6vkk`5Ag%NvdKkyS`^c~w<Ja#`MP(a3'
    'xUWU}-Hq>KB_wLPO(}l*WzdMrA|~(MbE+8U{j6X@K+!e`!4h{oa5&No+(=yXK+pli0qg@jDB5<SN*fKLFT-&FEs#o7?KG>OTg}RA'
    '*dq<ZW;21EA%Jn2!cI9*A2i{?kO*8tbp#Ea2+-7o5)@`mgLBe5x9hBa`#39L+40Ns(@PO=uS=>!J=;q6>+EddU0>5<W4--Hnb+H2'
    'KLNl1>A9#tUE(w=6F6m{t`@CSF_I;BNxGyc5kV4AAc{H?xFDczDVoi3U>J66d0(5&sAk{U#=H)@Bh~&lxdV~kVv`Tvn1xL~nDhGI'
    '<PKzhi%mXwV_u8N@xgwnz-FcOa&nD!2P4oWpAzhgw!~&Gb>bPw+z8-up<_@=a@e7iH4(t*aF8NNu$8QlU}S(%)teCrmdZ;_i?<K('
    'cM5<;K7*L}>PEGZ_4f68E8y>+Djj23&MKsb%02PdVEXJP&(%As-_M0dGnLm5I?6ly?f5EE<&(J`=Uj<anB(*bL|wJW53}9hHa^&<'
    'Jd$mE&1&`ntDJZyqv!O{Fy*6rC=mL8{^S4t=iv2}rofI)5kUyENmA;J1HA*(d%<W?a6mySdOt`ct%00DbKzQptzZOC37{^BB53r{'
    'oFZ0&K4y$pP7~0+Jg7$*CYV`da8go-9HZwf5^6XJS#t*SN?8EH20@M7$tgmz=MEG<_z#0YA~go~BpGi3p`{{RgEkhJb^^`@o_L;s'
    'Z%E_<v>B7#-^eK<gJOwTcyAz5$xOW~QG)2A1bhOm0zl9u76OPnC%J)LbP-elAh5_(WGiQU@JrE+b1rTiXQ)vYR6iI>NJ}IGb(iLt'
    'h_~JwfSYoDlB)>N1~{hSNMSGG$N}9bX9voc4mi0bcB`bMCR#@eYtYPSA3>BF7Z~*{0GDN!B`n>EM3V*TwS-16^91n$X4bJArvtb|'
    'H_r9BaaQdW=#omN3@7L<0Sx0pI7MSo3bmlr#KOHq0c{(u6jh9I5d6R>0LRAWU1#+ZI+xJ7gwCZdIIsUr?m*jXv&n~U40`Y`+Jko_'
    '^J^Cm9_Xw<zpxRRTP-pLA`ZcNOLO)yvP><gPziul1OYa<;$8`*c#y`azzurvy4Q~T@!%co0O{R>clj1pM;^QmW^%9x?+8}ljvhQI'
    'Gb}NKZ15FQF&lZbnN#bC@XD%4j0)Z`R|Kb&)(0JdNMcq&!*`G7!DG&7*azSinFtq>mtnP$I>RwI;W<b%R@5s{m=Z{yAYFhRql&{8'
    'fy%s-2QPBwcqJG;RX$qg6Lrj51OI1$>dprTx>=6E&n6QG+L?*KAO!e_nko-(lLxPr>w_faoQeYUd`Bc`1=bQ@{I^DeqDXjBpcgqO'
    ';3xu!7q5UFfp9pDK(xV;0~xb}EoevdIS`YyB}{1VfXYF;F1j~i#6|;$s)Hs)MYh^HUuBa-6@-t%coKb$fE~I%I-s-FvU{tIdNhUT'
    'NYuQ8Lc{<i5*VwRvdq0HXBfQ5#j;`mEueI}2-aqVZ!xB{)+L{tkO+1x3$y|@RERp}nSK<agXKx@3en}`Zbu4H2QxWTAv%U^*ij)W'
    'nH9kR&3dr8RZg@xfJ(SjSPg2SaiA6kDFTD3fytYxa)mxav?>X(jE|-eQ6M>H1sBNyCjje;5`$JWlH$W+3=Do3sAYv&awg0X*f^pc'
    'VW9C_<#tktGMEMe7G1X0M*>2RfJIAy>6~GNdsd7Ws#P|DCIMRj-}hic1kZy5+Q5(|hBPsxSq%eHE&EJ~^dMhA4oaXq!BUZu2|iCj'
    'fZge3WX@700~p~v@L)826Vy*B50oz*aB@5B*2SaEqYtde$doxz&R95h17cEu&L?arp+rqV)dRU|tno&|KOi~)%WcPceFt#4AXjHf'
    'd%<n@5=Y}k9es;*K0Vdnd!CGHrM2k(gFpvGdzwYjewu~9J}(Er+{35oqkH-^Q~&Egm?+<>HQG8+e&QacwL!5WP`73RY~=*Rt`lH;'
    'Pk^tSfZ25deES5NA%1Cw;GgA(A)Y3qh1b*h>%(L`ovs<7`O&H&<cd|S8)Ec09mUjQ40x~o_}<D#|M9KOcFo^8sIS=%?nZ-!*H=GY'
    'c}UarwKib}(<<Cj|9YB$t>O>O<>x=flj@G{Kgnl0B%j5wc|Q2^W$|c#o72|vzSly3ILMRnLqq!2)W40Z5rLK1qRI4aytLInuVI;W'
    'Z}llQZ>3AGeR!FUisbq>zJDp2Dfui5ey5zvW$v1t@7?$D%w*plV*aW8>nNfd7QOP-9q2JYx+`DZf%pajSo!M1<?Jd?Ny88CEc{{f'
    'bk2oqp0Dw<%pL;`&Y#tAKYui%qvfZ&IcScgUGLzaAwiS?e=3=&o<=ZcNifuE6Ts+UBq~ijY`zW=&cLw@29|JO{|F#nD<f^;p!p~d'
    '?%Wf|^7;&Gwk`5;ZYmP`!+-zRKdPOcJ!K0|*$=<|&wnpH<qJ>w55NBVFaP*QIf>Wdl5^-TJ*3<5w={uiq&hdGKJ79k_xR0KpXKs>'
    '4tQ~wMqlq>r+_*E`9`tSeE>pV9lpFeM^hI{+>VZ>@ss=grK*2Wsxi!JGJwcQlk%&7R)4Q6F_15>dpo8Mw(EO$OkKX=(2--RgPANi'
    'rXJEW$lGUEi}q!7fhmv2_cP=GUFg;WW5cXw#UD;*3hn5|I=DK^vD{c9W>6`(B1(e}AdQA$oyv{}E2SV*RGepQqD)E6Bcm=+Z9%Q^'
    '$q~An8>?iXXa)3PVevL(V0aN|aG;6=PEzm|)=ISJ#L1$b=;&#d!ZM|zh5cJ4Xg9biJcCnKK}pGU6i&hFbP$;UvN#I#n>pxIjFrnW'
    'l**ix2HTXYsA1bJQM<(~ZSc-!M<b9Gja3ejm5gyvISrh30?o+>T@F8IlM~Fw6AjWHsDf*g!>((SUPtNRoDwn0Z~@vI2R#>nG;yy8'
    '1G;jsTEl3FjDteU1t=DQTLHKz#7=AT5=cWcZ4_{|SAevV!YY#oeZ)9vjEIhUt~m9K5+7ysPEeAnyeaqu-H+&gME7HTlpL5o1W-!='
    'YAJxK0_;)5c?$f@B?Y!-Ae2fT3TQe$kU&dZE3lYiR&|=o0kXRTlxin)Wdub?MWZ5j06>zE9F=rHx*!5Qhd(gE0hC}{ql(fq5WYT('
    'paqbzp?OK>Hl|8F1TT~nA}v+Z2%w#h!fzy(tgLb(2%-WUAT>5iBd82cQ`mN}s6Y?(-bJ4P;)3LGq#5*5;2#@h*5<%1??C9a+j7t<'
    'EDM4%mouEB1sxj5)hUumv_VA|H0Y|N=wS?Gcm|ym#`aPr*pOJcSl4Vha42U0n^IfNHKDMY%^WHnH7shMoz@W$Hd~fp^ED`kUfL8v'
    'n}CBacUFNC46#gr4kAiwHcbw2;=nEMKwh=fEM+k^Z1x}`z+O)bM6dvG4xmpN%}BX=nj|bu0+ECAmu$&LbU{*}M(13|PWBFzxyx~~'
    'Z}JxQ>>)f_>d@sW%w5nA?}mO_geaaH-+kx4{WB-d&EDEhNcZyWp)N?5bwj#r=ja+eM(C@D=oC3TI(4>Tqic!nbRWIh4N8vWe%K*C'
    '!(+Kr<qC{Bl0jVxASjD28#M=UTW}^>B3QXim|_39D#e{6<zNDN9@w@E**(O_j~BRQRpj=4r0MlUv7X`C1|&B|pada{;ird}=Mrsc'
    'Dqg=2Mr}3DKEW?GM5^l8$+z_A9;;H<ue(`(JB6^iuu7LhSbq&t+1e7xDod)@(*(7Om;7=3FnV;QkbBw5N1)P4^S9TJA+PF_$7vo<'
    '9_k3z=5yISELgvw>$qSCcIPf}!C`0Ay2^XAnOY5u)t+EidWUw}+b%p;Jre9;A=?E-=n|7$UUvyO9~^`0J>-1(&f-Ozsz}160Pb+%'
    'GXCx85ft@CjXCk8w??OD*)2=)=??%^HML0bwz;!EQ_=PgW1~yGL$_G}gxSHQw-RQZvehqHV%VWWJGG+}oepNwbBfLp0x3JD=r~G1'
    '{3oqk$S^j~K*WN33v^vln1s`oJ5j!)0Epoe#jP?{ra*lJ3Oibg4oLT`31|U2GZ6VsBq=S6mMc)3Kve=f%?Vf~m11y07yU~I3u{Om'
    'fJMZeQau<f$OFqr%4G0N2+;hP0A(n|#I%J)d%=~>1<c_eMG}{#cm?P?KIO!^xO(qkUD7DvS*AP)xeSXcTns>IVcCI#GQ_nIN&&1W'
    '4@U%)&I9vB3zO{7s<s_$K|7-NEP=wzKnUbgQHZ;!LDK`p#S+j&83EK2i~*<u_gZPK3nd0qiWGyg>OAi_de{M-b#e86H{alq=yeC*'
    'pn-pari1`CWv*S|)CPc25P?D%Lo{fg#%m2?y_gon8E!x@Wg5YNGD+d(`tG@+pUd?JON{=j4P894cBCM6Fq1<s*B=A@cT{=6Y_y<;'
    '1HC4~tSx5@x}b7dh!WC*MO$4J@&kLCn2?k(o`RSFt(wy4&Cyhz6qQKX!jTvTnmNph2Q5R6p!Y=vTm<B+fJqVvCHQIpO|aCzry?as'
    '1k<}+t~Z`&TUsPi4JHAci!2qMgyvvqOXD(dgH=IF8mKI?kOE&)=F8cXl3aUdx1MR{Wh)(xTIUL1SPKXUe}G?t9YKI+2y<Rgu(c?>'
    'j|Q1s1clxxDVSg`1;L_Bht@6Y;LO@_sZ|ap?S-(>STN27s6-@kDY<lsN8zaCr7fhFr5H2!pdb}K<%$^tNMSnG(K=v^OQ{Tvb%oEP'
    'uJF;4+m3|0JE-oO(JUiqX)G&H(1D3a_LL>2QgEC_AA_^T0G~(abe5nf!32U7RB4!MM%BH0u<S>5KUnkWU3I^Ff!o#f!^3BCsOo+U'
    'rC?XpT?q<sWeACI=+UTB%r*s*i1Vgoa}!Hq)PVVbV4+m(;yKEgEzw2*v>&ffXaxCD>dAqk0-_D<EE>ROO5RJWjdmc3iWm~`DFazY'
    'WdYp~St{lxu*y!VyYf*)ZfVhqKth+GZBB4DF;h@_lmdE?)CUG;H<6__unk%(6?v%wtenUG#}{F2h!Ux>#>X5$qhbzTMgXf5D84qr'
    '%PCM=xyUPWa9S`aGw8dZ4X31iX6DeiP6t%h4p)dWHo`6$ZU8?(sZ^P}oC(jK(4-BwSy+%n0Bc_GB4%VD!YJ;kmmr_IuElydD`Oj)'
    'L)y@w352ax{x+>uj%H^bNlDnH{<75GF&|31N3AI}n#SN|Q~=sGM&li*ZWPFrmPQ^VvM4ktYeEukoJtPu%<ko6Pj=>^YI^T>=H=^J'
    '9od;3%;Zoz^BDTzj&>%jn3`Z5*tDfn3Y<4=2LL_D36eR_FegocShBPNEN@tQ0-7W~#gZIoC>E~4!Zld91`F3<;TkMlgN19ba1CaV'
    'W8oSsT!V#cuy73)uED}JShxlY*I?lqEL?N6!Zn(4L8LRK3wF*i!n?g;1Wt=5W?0c-Rmcn!FJ-{Av%+Sj2poiqNy8tnx^NBFt--oA'
    'Shoi2)?nQltXp%&b!!d|!C~DRtXqS1Yp`w&)~&&)1J<p%wz@T$>u7-#Kmm!xBM5&cK&v5Gqd}oDF)^jfy>{+gNnIksDxfuGtuJ}q'
    '89St7hjh&Jz)X+%FrcS_Z9pQxl*pE3YKZ_vU24vw)EVR?XF!R9fgGet09<FlI~8>B8g%|}DIL;F0wugFaxMdb{UbJ5>L?MwRPa*j'
    'J!pk6Oi9!Y5<ruCU235?rV6?;?2wKf(y>E2c1Xt#>DVD1JER}EL%P*5P-THOKocof`4pJGEcmFc<uI)X2h58N9Xthj4hl?2DRWZ}'
    '#WN+aL;7`I1?xhIJI@a32ix_%yZtZUaJZZf>D5KDj+|J>Foae*vHTcpPhhF1Pd-UTw2cmE4~SLzETso&Dr10w89C4?wYuW)cI#|6'
    'nM6V;wS#g}%2cP9ImoAIoD;@p$_dR5o_cCL^TIF=EX-X{#dFRS4SQbe{fO1u?x*gs??(cSRrCNu-NBs4wVHaD>TP&>@%N+9gfOaa'
    '{`vSx$kI;>_EwP+>j9+<SSg9%RgeL+(LujmKxWKYNaNvYbe|?~tFNWOsG^v~tuKdNCNX}Q&c0Ma93bL9{CMRSNS%b_VD4)LN-eX7'
    'sbqozg<WW;c%|T!-%an*WcH#-3TgE8T+{FaFj2g1<bFftW%}dksEqWv`8B*JOp+^+mSV*W7-8xYC7&4om6034Z>0RJ75O(pZb)%n'
    'rf=m%qsrbIutDL-nBc9?732OZeP7&!o9Dsq^fmkbH2n@^$f5i)U)fIEGf5z}r_Z-{4pw<HFos&?iR6SaQ1L@#lx3Qg=iZeXxd8x>'
    'YhI`$xu8MA=hT=|<QYsDPg3>9>C64|ks)IF)s^m7pI(QfyzY4DYoK{YES`~08L()^>5tnQ1$F@}z{303{H|f2Wdz?D0*{BvyHfrE'
    'OIU@rz%;dEWQGoV%ZOFLw}83+<uTr;Pjs*Vg!SN+ujAhNbYEswDc6NOCSOyfI;8tJoqcfoFpf)~zQN~Tdqd=ba`7EpH@4!ZZ*;H*'
    'wG-e8V?t`8K+#JE_)c*jC?!TYX5}(!4}ecf0#%u(i*p)U2ja6c{#-sEJ#R;D^_g<B8oBvu<enYTVVJFkY87U;*qnEmZHtRVz~L5a'
    'f-~W%mXd%8;SHEeN`YCVD6`qYbi9Rkfz-}`@-lMkLUZYVhS@HJ^i+1Mt?B@RA~;ZvJqJ@Jdc{}_+`vx2n|X@F`%;g`ajT7FB1f&I'
    'peAJwszwS`;LN*ey4{y}s~;bL3Bf&%A7SA8+nEl15o)%iaK14RrCHL>sLWJgjL1?9-Gk^Zl@@KJMsQiS%NPz;iin<N6D_ZKte5IJ'
    '`<ID1n6i3Q+lMn4YT`(fuN5Ls#(z{Qz{WkBCo)X$Mo({=%#YQk-!wnFxq)BA5x_-lG|o-hzuwf80$HnmTTm?h`BZ+o;q8@E>bZ(j'
    '_-SE-*Vi{mRr%7NX4v<+9&iDY%WI2|tv>ENSEk=hWH=jR#bDJhr{nu?)k40kK8q^fY5w-X?31|{#*abDy2)rpdJBO4HtC1-;A;=&'
    'm&Qfa{PB;+O5n_2{xk+iDlho@;wJG^^Sg3N!vR3><om+p%KYDCARW}#DB9@*Gg$c;F|YJ$*kjPdBN%pq=WLP?R#?WkF8ydiFsg(f'
    'c+5%rWC8Ldw_Z6KQ<a(*0l}I(94mE<wJOElkUW3EA=+jOz2c*OP=rCe1;`xF^tnQ2MLU`cwu%PR)7(<6bO#`U5dd@fkbGIt<mYtM'
    'R3v<Ud0JH8R;X>j2H~zI#gxaBB~pidnbw5$&D|GgX{{eW)vvC9Tz)&l;5qKTW2%K0Y{%n*R<7}VeOXd?|60bZ1uxm0k18j`X~pyM'
    '300oxR90W9bQ}F6$JTs7{VJzg#~uq=jVeBB&b=nH+XcdT9Ujvn{z$X+#FlohhQnGj{yd-f2C{p)`#!Cdqj&T-N5(w*Gu57^rRN8n'
    '_{VKSay)<>T?VCdH)`xaw>)QRJgP18VVl3qiKa5IP%-@5D~5~5^4ILlXZpj3p*C#V>q=DxW<7$fy8WZ%PQ0yU&UM|M%vo*oe93<`'
    'u(wJ1YQcRURk+#GhwV16*1y`R-7r5;W8vWH#E3?HT|w%9TkUl?0OA_oj&r@L>MzZ1l-+vm9q9k2Mnp%R?&4VK(9zw>aoVfrX=lf{'
    'bU9?1S;_nmQG-d4Nogp{MY2LO)3;+>61v(kE>=6nn-e|EFRq7rWh`_1-n{dRb5NT>PbHvp3$Ue<V_ZtZqjJ$_4?m5PI01U65vh2{'
    'xif*o%PE&MCP@$Tiyzzvb(3Gb1+A~JbdHLLo%_+T_c!+b#@^r9`x|?IWAAV5{f)i9yX*bEGE_T*f`S9@6N)$BK=}b#!QU$|VA+@V'
    '_RgEx>lR&k^($}Qf4uDBYc%-J^TAg7AeP*K^S3T7Yd(c#kKmd)u5yPg&33cLu=exR`>-&?U}na?eYC-y_-5WdjUFFU{C{Em|7S9O'
    'd0J)YuJ>RK*LuOi_`dOM-7L&;#k*EGmI~T{45^GXH<`Ha)#ax)L(Mz@Ek|j@;rcI?ji0;QHaihEPI0SGaCi6G!f0=Y*=R-w9^oR9'
    '@ASh}9C0Go^czupUesES<vks~o5$LC!H3y33fSVr*8X@l-SuBMyA%wNG^O8|WSaX}n$4NuGAosulx0*zsYGTeB(I4C)u8l3R-6)>'
    'nMx@zyCwx=(fPGw21c9o$<-McM|A+4Q^v+TtWs_1DyQh7QlokQ8y{@o%-P_JeEfd6?e83{<fFIG+WLJLMq0Z&bDkc>Mef|TLHh}T'
    'ZIjtUCbV(3&zzMTeC==Q%}4+Gd^$6&d(KQZN7l376oCCb3Kib(x85bC{pN_u{2L9Or(Lsp4_n7)Hle{{YzBQ4t9%qk&1*YH_xk>c'
    'N9Pt8YZ8^JQjBMinA9k=i<WeIiMUrr(g$m*$L&WJI3pJ5B3JM57{vj(dbKBKS(?o)=N~H#>&v1B@s3@3SDn_dW$*LoRe#w(p%y{l'
    'GC-h%D8_aWphp6M-t37r%-`Gi{b|z3@R!8!`&=05B}JcYOVNL4!d_x}KC;G@$-)k@PcDx$I!HEbd2t29<QOK$Fgb?FF-(3vVe*w1'
    '(wgvCGt#U$(lXgBMDLcEf6XPfRb<oA9X7-{#JRJ;IRvfaf!40qJKu+m56;40%(Yz?6yEXL=rWkLS=I^T!Mj9gcie~7vSIP|?8c0#'
    'V_lxbL?TQi!bBoWB*H|ZzegfbcO?coa>=2vLSco%3WXI4D->48D#Q}*M)&bk=Y_}H6zu5rUh&TLSZL`fFHNwTIuQl)CaD}<RI(KE'
    'OT6EDEc!FyVs$+hxw;<9^1&Wfmc<Oss<JFCw&-11mJn&A34S7@^E!KHtkKby&~uWB1Ho`h6;qn(Bq`5P!_iuzwDj;NcVJl-2^6Va'
    '&PI9<w33;PJlf2ubwF9lsz^+!oX(trNl0mZ(2)=pvkDr%ySZHXc9P#IN1y{8vd6M4%o*(h;|Y!tC>72hHclI<b27@o^o`_<G3U79'
    'i7*-@oSu+WaYAE`d8fR{$eH6+k3mnBkCypF9c;LP|1%Bn>x0uq=g3_$uy4i-6Ju0BNs^i>!QtStEF>xCR1_f*M`TRKTH=*c%*Y%B'
    ';YoSwN(ooyf&)3Ma-cxzywJZ1jvQD#po1-FC+2bh*3_0Tp}q4gsH{uNN&*5d8aPxPG*Lc)xU(QpRPrXOAbb?2^!lo+G;~;&r2{(a'
    'qAbhi9+bNkZ8;J(?@+Xb0nVi?0CidB0OD|l!HZlPk}?2CV?m*z(nt6f18mmXs4NJHi5LB6@3vi6TePJMp6RD(%fS+{_o6MAkGmZe'
    'ZRubphZb!)hHTigzs71HZj(_qi&d64Y)>Pb#W7cgtd_FKwhXm3i|qZpx~atc4k34qc7HJS-w<{wh(9~lP4EA)aYwZn<ih|P-@CZW'
    'on<Di*`Om-Kwdr4$!lSNPNmCDGsU8phBE3M(L5)T0*~A%Y8(|zF-ob<0YJ$xnlsh#U!$UPYiePv@IUO@l%z%B)LlzrbYOg4L`jV0'
    '43s5oSR}qRps|Bn{2fjlHk}XXvdcu8Nwo2ozjLAV>o)H|z8~&3-)83=-KpMziqVNv{fOFDOQ-tr9MRjI_1k!HaUa(@_K&AVw%oxp'
    '4y)7$Fi8*NZ|QUasMh;<P#A8@T6+#I9=Au<8Ty0|Xkv6`L7n|;i5%sg*itQ#TAAMwO?SU6w%S(c@f45~&WIB_xIx*0fBjNQige-1'
    '>!PH{5komg8SH=_eFy><0RkQP*w4MBNC$T1K_x{x7{5OsJu>_yG5kIyMb58q!j3M7ODwX0Y;<kP^Wk|E*Ud#8B;X+zUA-{srJ^0j'
    'igs6D(GEHOLO8ycLbd`6+5RmH*&+&@XAtH%Vm^LNw@oFD{{67k=(7lF{|vbVZx29Alaae0{jX~-)^12J`*E@2({?CCm8V~o2R?ZT'
    'V9!m*lm0x6h_IKAu*XrW77?~LXZJF)_QvriLR3^KPoYvSqioO0!<v%2c*y}TIp8IS{VSQylD9XH+ish?Dp!isxpUFXlTg>LB_}sy'
    '^nnEt-%;XXal|%PXnL=ah&M~{W(nRb!J8#`vjlIJ;LVaQi%WKtM1fKsr94V`l=3L$QOcu~@9O?&M^!oN@=u?B>98bct7474R*l-Z'
    'Bqs|JIF{M~G$@=D);RA{mVr|(X!3oR<TQe=F3G7@m*iYH)x+v>+M!)lkF&-8ysO8lj8|D10(7r|A|wwqX?O=GP7z0vh%Pyz4ZJeh'
    'lzb9!xY~FLKnsU$539%Ng|{kY<}!e_wI!{?N2y#ELUKca*Q`$2P@WwnCL{%jO{FF*1xlkg8|!hlp!c1E(jBPXJ=Wt)QHhi-V3}dC'
    '4Y02~Xc=-$Rz$|FV#+HZx0FFxDiMSWP6<Utg7X$k?-Yy6AV{?Zp^>S!On@Abu;)o=4(g0FE-RI-3R2QQWr;mgpm553ktI$?y`>)K'
    '`c<01q!z|9;FWBx%rR>TqCOFwg$mSxpyEl25G6BQr)X*7i~uc#ET{{EW{@Td*wIU0TNMxmx^YFISQ|-#Kfo`Ej|7AbVa^L06Q%Gz'
    '8YG|>waF+cm|!ji!J@47fdkZ$4$iDyI*e5gChdg)Wz1^Ng@m1r9Jo*d7e?Wz<fUV=mxLP5eFA+A^a`$+F(6`1#|1<?V2l;DE*pKN'
    '*fwr5a)28mW==qx0`!VRN@u;H5{^}i01A`g60p^zC?zqLa1J}2Z7>#&+e#l|!D?3*ek^1B`u(ZO0eNs=i_UX5zOPF`HYR9_l~9=%'
    'hMb41wrW@1%@^MFH9e-fJm-IudA<GhQ(~Yu2>@Si;j}^GfJkC7nF#o-7|9aM9v}xri3pN_%qwk!0~e$yqO>A{BQPrL*z&$Mn^Dca'
    'vyFK@TIJXy;qDHVV>P2$Mr}FEN^1g4M6#zWF_nVjEczInH3s-RI;XSLoWKNv6jT+loUw9j_h8vi<=BIDx85tqUcSKXsB&xvGdZ+!'
    '>@k#r_LXBB#quaZ8_W=QKmPi$rh;RUSZ_g*V8Q309M2$`fVz``_X(C^yKTvB_wZIHGYO0-1Ar;Pa8UwY{U%TA1$HZ6$Nr5MyA!a@'
    '&epH(PNWWJiE(GBcD2gKtLA;v)vOm&`d0R=|FNv*rk?Sp{J(|Xv?Z11X5oWI4zJ0r+4Req*k1S3?Ma7?_Sfn=v(wm`L$fuJvD-7+'
    'elXj!CA+7Zl|h&7^s_lnvq^fc$bYdp_{?|v_R(h-OCnZ*;}w~5J@(<glBB!^U3K`Tsvf)=P<59^aeKXXMYruGt^D9=Hd_7ijxVXh'
    'hwsGl%PY<L@Odx2v<9%lDR7}1^J@Ay>Oe&otEzo>K_%s7|Jl*I(xdbDdz{*;cR2u~Z>bwb7)`sI*WD<_^w`b3(tUdFXj<?4TMh?#'
    '_co++z+U-|m3Ue~K->2=fbj>YMWAAK_+s}|@<i$&SCx%CzVGt&o%MNblZ?C|iHl~?QU=e{wm0{VeBEanvn-pF%wtQs`>x62ZHMh#'
    'K|P!<7PQ&+w^CMOO6ONq){Qc|<M(F3EM=}Nv?E^gf_ZD=b<Z2C<zqReHWv|w@0U^3!<*&F&!zh3=DJ~*)9&Ch#c<udjt}-_FLyhJ'
    'EhyvtHMS4)oi~i$ywmUBvuW2qeD}ON!tg-6;W^PvSx7lp!+8uPAix;>*9)GM@HQounMu+qlA||Xnc|evF0bhb^FH@w$96Dn`nK-c'
    '4yJ?H|5>(!S=V=c9ZbBHDc`NXJEjc}x|+mh1Am7Z%?#zkrsLX<zo<zZHsnf|!43;w^b#CM5UYI)yBtCnvsiXQ?6MObjQ-AvunS8p'
    '`3<_`iu0J!NB>73yz#+N(#N15JSu$*`rh@@3-WyM(eyFuduL8BD}C?vIcU&M{66azwDOg`UzOQs>T5hr7cuvZ&a8`AL1+H5&ARlm'
    '5j!8eGIme3VcTat_-L=rKUs=XoW&yEAMM;U2T+?&`&>wKXJ=rWn}&-w3;Mlg*g-PUhigHYO^eyI-DJ}q{eEc&iEN$RE<9pR-=X(Q'
    'JGh>C+WVykpK&kuOOJZPxRd*({oT_&;(hf_Z=ZHjHSlQNOV9BJ-Tq}=&+Y!}{$*W{(q^WPZ!M--o_X`q4h{hs0U2Rk8{e&&(8wOh'
    '9>^Z+)BTY@kU!3xKaSDsdQp#BDn8t{!O&{&<(YQ6-kuEEqXCN?i4tqQ#9F*%hO!Acp+`>WptWoVWlOeq$RlUbA+L*z@?EquIbwyJ'
    'qYSoxhdcy<%K(86Dp+g>0eaHLxE;jP4(bxu!T9}Ez(9t-B!=IoF)ntLKj)6}$VQhTH#?|OcHLamK_OP;qN^81Bq`@q6d@5uWK70d'
    ';*|r9MCKp}Ps&s099XDee9GWe4pc|WX{5;nM_REFKdJiJLA!v1)XxsaZ>N5CFc5P5^{b!9h>*PJcySh9r{l#)dY?zHIfIA-*A|31'
    'j+kHU8s57fcDZ|a?+kgOF5;;299!jC?ik*ahY=AL6Hm4wY;Vr)Wn}G*<4=UBs8XInrCdhYo|T8DQc?k}>e>jF5XTbYSVA02h+_$H'
    'EFq31#Ib}pmJr7h;#fi)ONe6$aV#N@CB(6WIF=B{65@Z465@--@4OUS?M&G=Zf|(BSm~}7GW$+!5td$8megLj`1zRrc*EqGZTd8N'
    'M2F!;I}GRSlKynzxgmoIsPFOikICpcy&=v#wuKG}bV#5>0v!_QkU)n7Iwa5`fewjlc1WP_0DTAOJ6P>Im|IAHHCOmHg9%o@US0iq'
    'xUrt~2@tlBbDIM_&orAuH*?u7ROq~Q8#~ydXBa!)=E?YB-rjA6zZ$!{VRtv|?uOmnu)7;}cf;;(*xe1gyJ2@X?Cyr$-LShGc6Y<>'
    'ZrI%oySrg`H|*|)-QBRe+f{dW8xCH;l(-$|dPdb>8sBj79l!Qo?hl>P=#)mMG&-fxDUD8PbV{RB8lBSUlt!mCI;GJmjZSHFN~2R6'
    'ozm!(MyE77rO_#Uy-w+}KVkgvG`dfdx2)v_s_`$6@jiW`i(14ag`(rdkJ`k~vux?bMCsM((@$kd<Cp2$dciD6I7Dg#;EixnSmV4)'
    '8Ke?GKTW>R{x~I@d@}wvip#rH*)pHLmCaeXgq(nCCG(n6z*SZl+}U6WNZp)NFtguH@2ZO$AYh-zlZ7&P6^(>6dU~#}i`Sm|hQwyH'
    '@wqOeJ1?UcZ;ReCV1%hplzd|RS4M6Gzmev%W%S<&xgn*-d-|5DJNA|Ct_PLMm>!<SaK_>LZGB&5;Z$MyZ+HEjO_qnGMdArI(}G}7'
    'DuEWjX#m|pEqs5Peun`l8B+4Lnw@rNl74?l)92eeHwC^KkjMmxq6DX<1>rddVE1fkrZIAE9UuaYKr8?j!7wi(&qn#2bTZyqQ5WHz'
    'zT8(^>Ox3QWy@Mqk^<7R#1Ik%kcLqq1(O0v#Cbq&M}W^_)S#$1YeK;(o}-M}5^V(NBeRm-`%?T)^}aiZ69xA;eyl1e$8mUhXo@?|'
    'gG1kMfg(q_s#*abH(zN;_k(Df1)SDY)sCRHm6F{`B+w|WOcbCzCGVvLM02137VS^Krwl{@m8A_>h%71Zfn@kjWsU(lfif&DZFxa4'
    '&I$CL1Ts5jDil=;6d*}`U{)C-N!h)iwNjD8zk_nVahz5Hw?4lLSevrK8&)X_<x0&mQHgqEa*=0II|`&jtdN4UWO#z5(gf0u(Ufc;'
    '#DmVvIa6e%B7X2oYca5T#vN$+M=%|#>zbe|Y^EszimkTB#~i?rVGdqKfc}#V*+zId1xgFRXCi~t6imu2Qexbuq<v<_4~7RRSRGJV'
    'yWR*y85?1j3^$-xg6LP7yPN?YJ)ub(ZnLn!-Jr*N!3$)|K!j1;Q!jz%7ebY@xZ{`CO=~|oD?hpK_v5;oz**b)!;iDlrS!kCK?pFq'
    '9S@vQlZ%$>q9-l~paba*Jc!A;L>24^99xbaEHXhV>ABOaen!v3xq2Rk-4R9qo7{n@YO%=&Z_IA)oik7pYBg;RrL#&<D}hvk-OGHC'
    'uv`r~ve7gKFQWp`wlRW63(6-2G6iK06uQWw00TJ+Nw{$;xz#{gwM3{w8OtAj*u*Bby>c5DohjuPi#Zmvgo)wcs~gp~V3!kf7P4#>'
    'Bk{F~UMHyBRKK6&+APfeK~Sym*Bn<)E@f~Ol&-TW$|66^_RA9XzV4Fab*Q27y1UJ`>hkj9-d?+|wZJMTp2<45+9~zH6<{`Rt<tDx'
    '+nTF%pc$>Jm1tp2HY>^uShy-B4*I7Or8r`wlxZ-l6KuIKN%81`iQo(bS5jJ_NGgOrMXkgR1nkOMiHD2Swarz|K)2naxyqrl-le(9'
    '&)8gLujVQpP}Y`%*|oV!7gV!*bCsX5xeC@Y#agCV%M@#wVl7jwWs0>-v6kt1)H3ZrLG7@X>Cf0)1q-C^RFU*uo5>axRNK5}V?omQ'
    '-^CiN9rkaj8ymOE+?pk3eDIq2Oq|G+3LObAuH%>$bD9KcWb{Ud2p<K<jbPf4L^*Rz2+Aa(+(_c8g!xJht>TR9vcc9Sf9;(IGk-yk'
    'xiD=qz&>Vjm5iNu3)b;?ULzFHy5zmhvybpTHr~g^``CCN8}DP|eQdmsjrXzfKK3c@6rSup_KD`+;eG5At)Qp-*n<_;?%PM5+|NFE'
    '1JB`pc9)Ad+WqWfkm@n-XaC&){p=3%moDReHs)?)?l$IbUrz4!(XZxrkT%)L#q1+ykREz9zk{3Yr@fkg@EP}VHUFr^89KR|-(QIX'
    'tnq@<y6YM*oz`SH-0)eh$xzWzDX}nJxc1*f+~?WEZ{hcf*e>f<`Erb_;=ok+y((XDm+Cth7f}`B4z{i$nA(L^;&`_Uwq12aV)cC)'
    'P`a{=+H#ha)+ClYu%5ESR0@u>=woo!7{Jo#oX%2n0z?y}06hxGD-S*d1?MsD#cb{!Wo+>V+<l)`SN`8g)_Jr#kN!+`9fkD?YdszU'
    'G6FKf8b;WD;R4wM*#p^Q{jDP859E*Y=8sgoB-v&r3`<VJ*gR+%GBApWjDzW;yh=Q!oHcwk2p60Zii(sR5lnB@qh_hkP44HK#J}oR'
    '(U&a|i+8-yd+4G)55CK}?$L9B9ElQZy~J9)%!;xJIiW{R00B~80H2PkY-X3AD<a&1Gdd`Y^^r5`c`a@9OuZ|GAwSROy^{btR9baW'
    'k7G4g_CAkZ12-S32|?hxK%j#{Iom;i9ti|`3#6`L{@%v#uh0%M{3S7b6S6$LLj0cN**cEzAjZC<-aLQab4N>LqpMb)4=;vz<y>@l'
    'adPCMs~1MpIwz$f)<%-x57Y(XBjL&t=DeUWQJR5%f`kpCHW?)a6U-&BNux}x6&vxBs-GRS*f~i3>|p$M>SqT7A;(`m#~&p^@}A?x'
    'S$Lg}7bEF?9=#@uA_`nv5au{yK7LKNwd#HXKWsJnEM>TVhFpTT2cV_N$lZ_r*ELt|Hk5jqm7Ht|g{boMtMb4nF9Gbi>3GtghY=AL'
    '6Hm4wY;Vr)Wn}G*<4=UBs8XInrCdhYo|T8DT3P|E>S_*lD!@(!*r@<J6=0_V>{NiA3b0cFb}GP51=y(oI~8E30_;?PoeHp10d^|D'
    'P6gPhprcL&i}v-r!ei~P**0!(khfSsvzBA~PHYkRURPG`U$|TPnErT!{h9guG<rmz<VE`==j@XHbm6%ng9%s&@%E3&=y?|GSqz_{'
    '&jWoP=<`6I2l_nF=Yc*C^m(Aq<C=XQ=$=6L1iB|q=AM`vVShCj5jW!%7H?l&ynVQ_9Qz4CylUyt;y}+cS?JKsT=qN_I&a;O6Sn9X'
    '#*VjnGJcphs$Ai9$3B(VrxN>AVxLOvQ;B^lu}>xTsl+~&*ryWvRAQe>>{E$-DzQ%`_Nm0qSJ<Z#`&43|O6*gKeJZg}CHARAX??YQ'
    'Du;uY$2h(n=XyrfUz+$(i66c8UbqwkQy7@Sz!V0iFffIIDGW?uU<w0M7?{Gq6b7a+Fol6B3`}8Q3SC_on8Lsm2Bt7Dg@GvyOkrRO'
    'rS)|NrV3+(@x#;TK26?kFD<}@e|e1e=@VVF9409gyDomTAby+Onp(UtwfY*>Q(4mZWxBTeK~COz$-Jf%md6S^I~yzk63a;i(`UE8'
    'a;t1I{x*usyHxozK7A{jvw8_Z(Zn<@z$Ih?UI|)A58@M)B%|5vchkG-!X_}p^LVmQ)VHFMkVa3>b!D15TGx=+j5a>kO#$Xbq2q1A'
    'nE@kAeWK(O<G(U;Bg73Mp9L}hMhN_=xM9Dgs+><{ubV+-CZ>m{F`RMuep}yLT{u-({##igXOrb2Y0wx_ky=1vN+sZ0P6P0|TKN7n'
    '{SE_AI;8Y%H9PIlB>n!9rq8!`ZVG%e;D#l^ts<TqVF*Bgmcaj1^JE3^DWQY|N-z+zDpO7cuyztMG6ig{Xx&m^`f^`wsS6=Jl`U&a'
    'Nz4F7@Cd@3+5{RB2a<_|r~r&d4auBI;z5a`9_A(;Q2<@i7(#=W0D^ATfMj2a->rMe;j!Z29><SWKZrOEFAq(r>Un_p`z=u9C|3ot'
    ';N#{i4e5T+NoGOk)jdLhph_!m$H8!*pa8)+tK<zB5FTb8LD|m+C<3Mqk_T)E&p=5Eu;W2f38G-9rVS=%;j$JU#&W7i6qlt3Yej3I'
    'KBJ-l3iJYGS#SnM$UyC2a0od2nBY+pJgGFT`ur+jZJH!(SY-#s(h)4MS`4h7aR*ZQ2%>(|jNxCB@}LvR3XDNR9Ov+k1Pp4Zx1hU3'
    'ViN(Y(SY(2OxE5rDVSD*FrD<u86W)8s+I+)wVSy>dB%hV1Aws70mm98xKuF%0$43IRbYqF?1B_hD$TS8N;X6(4J05z#TpC`vIRQe'
    'i*~)K0uVrQNdwr0;I!jq-3GQ$%MAZZ1!a{}ptdkzaz{f3;|Pp-0y+$g6c-XKX4w*7UN^1%=-~6@zTc1QZcgZl+}RVkVpbk>TB3p2'
    'Y$mWXrBijLuv5+eA`l)7iNGaPN3Jswpb`s^cQA7roRi+UU1#+hT3qNK{Tus7huv{r|C`)_%x|&D2XD-7zJoK+Xlt8dE=6Y*Qdg>7'
    '$)^PSqAjtROPzQIGB*OaT<92-k{os@Wlg{iwQ!IkNh6sx63isy^lBMebt9`{@5>*4*u*9oYvndBx_rwo7Lx*J8GggTS2wClw=8Fp'
    'E~JGnrjFH)#5$w$ruzL{c+B#`KIkYbyvoN_k(N>pizuwq|H>ji%=XKYfP38`DfwWl_;q&&!>Woqi+g+RYW4!FoOmYdod2gZ1y+FB'
    'ysKfOnQ!ZB*n#@Fu7<dPFWTari7^r`gLYOXn}K;0C`hnu&dT3hfWL66m7+RC2mdDC!B5F4YKV6rU{}@<KU}1)?Q3`j2E-nH4G*35'
    'E`1Gu!@h=l^)>8(vbG$|u6+%=pqkzL8vcfT4Y9^Q*7(O7|5)Q6Yy4x4f2{G3HU7_|#(xJ2YKJxcf5X0p*f?PC#sRH*z<1GFpw&RF'
    'n-8>m8@4CtuzO+^D`=O@ILlR9kvt+eO(8I+Rq<VU<*gx(QBRY#f*PBB3_5vFyezR&Nqvf*YHcOsq;iPHO4+SK#_JL!*XBd+of<ZO'
    'L67NSZIU8CCO4KOws;FR_;_AEA7nyF1)V44VVM#vQ-WnmuuKV-DZw%&Sf&Kalwg^XQ(Ub-S(%a(&Ar1iB_~=zuVqTA){SfHl?(@O'
    'W^N~iOAg-Bb0}QW<syz&xa1hbd(6Tmzx7|Zq=UREEL?)u1@O86UKhaY0(f1Zlj{OUuUXSUrfw${OOBYdd}z&@4obe9wr0)2XWUE8'
    'nxj@N>ZE2(f3<&5eZ;zVsHmZ$)=}NNPRkV??(SGFS5z@;sXjDasEE_#_ss7uye0co9DP{@SC(Vl70jgq^;Kn`yHwx!x`={Fcd&I8'
    '!PKto63@R~@aR+*vR2>D1Mu>W`B3x<4a+f_#^7aCAQ)|o#ygk{1@xgsr6!R@p<#F-2{#V(`N4<efO)RV;@y!Q)vMwIy!$?_s^Pp-'
    's^(cMdGu$hOAM_~kL~dgkP(m()-b~M>s`nm$R5ZZ>+dike;|LHIe$d3w+wR%v~Ms4txPlspOW{|f}P;Naw}$=fKM6NhE&2_bBHV{'
    '?_tG!s~$DGXwhVvuSo!|?l6AY5~qB}8@&fC+w<VN8y-D6t&k&8Vy%~0i&y7SHX$eU$O$Ruy4=KcRNZQD`MDy(9XO+d3XC5)qn_8&'
    'M$go{QZxDUeBL_=utTL)7uA1Ob7k-I=rv~dk(v+$t_uV@D9g1S1n7}Kptl(M8s_h9{Qk;%A;Vu1!#C0D(<{X9Ii9WK_zu!5cGR2a'
    '&wFn8j%;+za<hZdtJlp%9rW!$F1mVQBrFr!8V?Gml7>0SK)G}|fqr5mm@G6gQjAoQL7@>$$}Cc1+$PX+GBdsv8}XB>pB=OvJV^cQ'
    'VElIKX9oiz$6vqtd5j3jdyW@p;dMG*jHLH@^qMD(C~$2-nB$1~_%+?us{0B2u+`|ZZ1MgXatYoZfR-jBcR%`H*IcmOQ1WS3;I<_c'
    'qRP{+$^)Og1hD6(<4J!WMnqUlJlTe@y*ay=k+nCDKM|s$N_h&Eav5cNRvwzFi3PN(`%l;x1^c34Uli<%f_+i2FADZW!M-Ti7X|yG'
    'U|$sMi-LVourCVsMZvx(*cS!+qF`T?i|UKAXvNQ~$JSn<ZR7R^tc%5YYiYUf#1`@Ob!DHBg&VAo>5n(zGBdKDMvv(EylBtooL$nN'
    'uDI|jGMIoJ5^w*QjGkv9rNtl}dREZ0f}R!hte|HFJuB#0LC*?$R<7Bzf=&>0f}j)R%AFu{Q}3_lQsrh;#2y5zdk_pab`>~*sHm!n'
    'w>Z%AOnf?YGnf5ah0a?y8HO!-hOy&qo{S&n&39Ks5U}?y_TI(byV!ddd+%cJUF^M!y?3$qF81EV-n-a)7klqw?_KP@i@kTT_b#?{'
    '!<KH?dl!4}V((q-y^FnfvG*>jkJx(`6*W}UuD18?aPaaN$G78L&#3xK6DKZl<k#MdF=K!l1JoFx#sD=2s4+l|0cs3TV}Ke1)EJ<~'
    '05t}vF+hz0Y79_gfExXt==a0`H3q0LK#c)v3{Yc$8r4S(P@|%TirRGss0*Bg@x#;TK26?kGcQOA|MD2`(<i#<FPNlIygBis-{7a&'
    'E$YP!)T^&mKb0koU#9CjI|W4((^Lo>g-k$bK?~4EK;6jEXx3*Z<8sS<GX6G-%ez$hH9vhTo3nBWDodv#RUn<B&>)Pc0#UEIWI`&*'
    '$?SL2yQ+W#FvRnCve2-gqLGkBPtSEdygF*%kl2hiKGzM8<_$06Z2|KEBTRjw<P+n+GIArt4Pl=ZW&cLV4JiYsZ>cIGSb6E}pmGkS'
    'ho>=|ark~)*RfbQRapL8F+*pQ<)8ur38o@woia)#fJV^lf!Ec-_owN17=W@NWpAt5X@@51_m?z%zP)qPyRt5BGUeVzP62O4k_@OJ'
    '3(bK`1>ivPI_T(?)JbL&r16XZD;u4B0P3kb!%biAt1WdQq^GiFZ7E3<mP(RRXB^;e@=P#V03HN{0ip!hD~wi<8#t~s5EDElVssG2'
    'BGR+wYWq_BPMz60$QcRlar{{Ix{BlQ^3b&5m?w&SzXggM<*F<veB6AcA>9w^=q&kUb^kLfL82Pt;rI#2PY(hv!xm>28JHtdha98l'
    'ED~xs30ZRnJDai)AY=tKa;Jt=Bzx{abi;ob3=*j^aH`38D?vml!Zpa#z_b&fr^6G^lL1RLasl$lR9iDwvg`A!fVF7=wPBSV$had2'
    '&RPttp0NyyC1T;dfdEft>RkphiY`jvbui$8uv}umNm)C|4eX+eU~L17M5ZEJIpc$0x@pE8C~HU105{FJ1MkTZTryS7FR;Nj^Q|+~'
    'C`(xxN=Qp21Fewen25LD8vx95ev+$LkUeS*M+$ohM-G-Q<!mrK$Z+j|liPI)4Jv_9AzDWZYtYPS4@wv}E->m@04~ccOIW%Si6#rw'
    'YYB~@s0-o)%xobwY?kNs<#p5Aj}Dhl?)&|??yZR?@0CPMR_zt&WlE-OirPc~!?+Mm(I_nUT2N|Y;a;MEwhdQcL@>rdbpb{JI5sx#'
    'I;)@1J%H{3jN^2{dHrv42ijhnO+IvEFj#hW!Ll<jziI<`u0YM22&~pxl?D2RjmX?;ktq;y2-aJgvyYKwYC(lc0IVViE8+aT5=!wP'
    'jZ=Y})#_8#7Pv|nTmJCFCN>$wE4OhmG*y1Fn8`j%OCJutx=~$saQXVf!d-^NeCImgQC|bNseV6~(6eOb59;ZP=+|+z<E1?LqHokW'
    '>t&H2X8UDHi+J7PCe6VvoY&pGVXMkTF7EBMTUZOMa^jh+3sRoa3S0qZ^Y-G6QMRqYcn3QEx;8u7X^><Q)U70q26anYScx<;3ZP3T'
    'VAB&YQE)Uf1)Jc7h$K*uV@_M;_$k`#bfCqoY_rpa-m<2__!(Gfdo&n7bk@5x82<?yjPKQ8yaUSGaxl9#81I5=c5g8L6E+yfHdxpO'
    '3)^5}8!T*tg>A5~4HmY+I*&G39Vn<Bw!!)d8;oObtt0l<8g6Y+(_8ngR#WM)KUcf)u!C2JZOp5PM7#XnS^D0`R1<9-p-M={wB^!x'
    'k)qcGFuG(a=A0#QUU-!?Hz8`RsTYI>DzcVZ#LDd&E0wy6{;$g!UYjDkcedpG1wCd=w#gIznD1NiI^!)^`r~<rDUduRJ9?g@iWR)D'
    'f)`fs!U|qk!3!&RVFfR&;Dr^uPElj#WEH$lH1`fGc%5hkr>fvJ9IUXR-<R@Q{8A@1y$;^wbExUn<syz&)9V=Ie9W3&KlNYJtAl(k'
    'EXsg4RPcrh-cZ3CDtJQ$Z>Zo6mGds=c6f4lC&j#um?wW|Ikyg~p`Ey#+o5ONOF6fr7WC_+oLhfo@z8R>YQU(YqmGW%fKh!sOnvOM'
    'lHcJ@&gDve6(W|xeA9(uaZNVi{HD}fYGDPNmsN{wIp?L4C8<dHs_5Nas_&OxL@mKP*t&{f>X25+>$zR<<5t%{*W3k8J|!S`ZHdiX'
    '>clgkmk|KoLdT$#<Usgf`hva*pq-6K(nw|vX#T`FJ@}9$G|zckys5UMW?el0ci*Q~p}2SY^gMelkN&KZm^LNj_jm}%2*?O)7-9Qm'
    'Qe+Qg4`h$^H>Qz4kU!3wKX^z%Sg^oA`KBHWL6%hV1`I%NRES`#<x&jBc`z&lEd?&9mjPxKG?id@Z`Gq_*SeZ}Eo&|UR5zx-Y{^b|'
    '#~Zz;DYWOoclXJ9^j9KBqQqJ+u@(z;plm`;=#djr&UJB{>8QHh<nnVxggbCX2c>>Laz;I`rH!7cccq=c=lQ&M5@3f)t1fDdt>((!'
    '=h17p_9HbR2wWEkbWj9vI|$Gtfk1ER6Kj~ixAFTcB8UusNetg4Dx6*+e$Vl29mjW&S+k?wJb&IJ<slngzuY{y(DQY3Q3oxpkc+Nf'
    '7^#w;XSt_@Ip7Zq1_+lbW?&twrKZX%L$eD~NU1c_+T?7AQraaWsSK^yh@V#dJiHn6Ve04M<F`~l4<86Q{^~jYC=rtP952qo>vX&r'
    'N$>OMHANgz;M#&P#}V`KYr3sf_Y?SGtI=l{1^Q>mC3t%PTAGa9{pf#PbFF?u>9<)q?Uqo8Do?*E4}9_xz@D3qC;fRC5n(a$WE;Zv'
    '=ImZZ*4{Y&M2Lzi<tbFkWt8n%d1$Jq7SO6LnPC?u?81ayn6L{Ic45LUOxT49yD(uFChWq5U6`;76Lw+3E=<^k3A-?17bfh&gk6}h'
    '3)6=#OpCsPd1dO__q1)?-r#$&@OLef_np`xiodSxO0;lK_A&kOCc<W>Jm?_kU%hDm>YQEDpRPCxD>9gXwG(gun2erhfvd$(Ao_36'
    'e}n!T^xvTW2K_hazd`>E`fsk;e}k?TbhV(X1zjy2xLW20<zLMO(aqS4{YqBa>?N0HLvxH12&AeEeTxG<&m^uxH*?vGR_MHS<8;`f'
    'XBa!)=E?YB-cWo+Gz9yJV?S~1CyxEZv7b2h6UTny*iRh$iDN%;>?e->#Ic_^_7lf`;@A}s`-x*eaqLxyy$Z3PIQA3AhLG4#9Q%o5'
    'KXL3Qj+O)V6Gt5#b#&|}j_Tvp_7fitULNE4cAV=ORex#nMM^%&Ywrc-F;N8*RWMNn6IC!#1rt>;Q3VrKFi`~)RWMNn6IC!#1rt>;'
    'Q3c)2n5cq@D(D19CpadmV4?~}8Zc1>6IC!#1rt@!a==6t)X`B#$3zuWAFne}rC@d#KRk`@)8wtX$%2yLUmoLq`a~DKgp(AC4>x}F'
    '7B+nwXSG!p%c`s{w(?XKHh!6|?Omy|bShGnw249!VW<KDueoFb%s5U4J*Lu>+B1{!w^3Z)t18Oz>08;Nl}qr7xj>1Hkr`21Wm*<F'
    'Gm*07U@3O?yXjq3)(P0+c|2KY$y8BFNTa9cx)yJp!_yGjj6y!wEgk1Az2a?=n*&Ce`b5bm#(!nxMu;1tKEtm48zDEO>|OensshH9'
    'hn|u_<rz*7Ph&X7@cp(fZ}s7DVHe)&DLb1i2PzYZCqaUB$|#jU7y*j{<Ew@5Pt)%(4&_72-xjsgMorT1FKPOGd*`Nir8PU%hFUPI'
    '2?0)qe~d^VZ@|cQI%W8^Va9uqO^N0R20tY+SYa89jM~_AKApbYS3B##_<t%p*Os6%2X!9c8W1|dng#rscqv1$Kr=9xn3&QIz?wUk'
    'sZa#iGHS1^^-3g`XI*6XCH$RQ&~}hW7Tn|bv1;lW$KmCn={Gjd9{YX^ggMGpWpMbo`AS2<AIy?jM%wB|ub{F}W(60?0abx!CkpT>'
    '8cC7BjDbu5Qn9QMUe1I$0{RZoj(~z<ta3Yb-poQ;0mgwR(fde(bOa6&v>4bcM!>`<>!(^}6KE33fxNdqCK5akE)&;eR#e7TeSQ_n'
    'Hk~dvtg-{G;s~lrEfZGHxC1xZ5qxu-X54{#d<30)(~LV%gN~qERb@xP7T?UbDsqqnBnk)0swJ6P0^nt(<~&NU69B*%P@-U20-y=a'
    'fO~^dK?kpc;$bj6$br!TC%0>f4yX(YI***oKw$rf4Hh&qB7mvjrPO;3>KUlz#wRd=U|T@#49q*GihN)T`J%Jr&o8f=)_!!jd~)CK'
    '$8{6&nOR(L+r7lmxc=jUN95_L{$7>eQIwKmT@M1S8?ZJ$fS+c@`{!lz-93DoKDwt*GnKhE<3;&atx8v#l$*HXNc!t_rJG^R1lY<6'
    'h+QYZ_MQM=IRUfl1o-v|G(-H-48cFk4?{ea(w=XAlKJbyWIUa&8KL>nsv+cxRjeCg^f(1Qvc(wiUi<OAl^OT(t<84L-#K_rf0h~E'
    'jRp&^uYSDpkf!Ntt+foMRY0fy^%TsR$M{2GH~8=mFl`Ez|C4;CL-JV+o9BZsUlxz{w>fPs@B6F?OI<vY%%}csT#X2<#L8#0xqcfj'
    'ZS~J<SZ3W@ePx7|x6-B8KD<mvRa{_v|ME~}bIXF?DQ8f_8_vFW-^Vkj*meimPxId0U5jp5^vYLvpvM5|u6%U|;u{EH<*N^uv#Y!d'
    '4L`hdE)1Keb1q!-e2s5n_84ez{;Y=k`J)*fEkFJ8dIs8cZQso0<E%~1D(Pr-3{+X54bVi22UF4HSnyF>%VAm(4wy5r%z~#N&q09+'
    'DP^!7;ZQtNVp%%Q#9-ClzDg3jStSqd9N3bR`V4EfE%I?56(RJ8|NgIkR69L;$`+ooAAbFx|6Y2^7oPGTe*O1f{_&5-y|gf)=ezWf'
    'ZpYu!1g4Q{&vt#<WlHYxo2x!6$*?yZ@Zv6wzTUx30d)fMjbiak0imz<a5dTQD|}qTrAS)&#o{fD*_Ddn;Hw+ewR4xR?kwEHS<Kb1'
    '<1_UYmKyWsVsMs-|3Qmd5v)6|61Q~6qr@rd`xa%9A7)!35l`;-I=)oyfz7H5R~eC2f3xcE+1*dmkbGGN<JTQ-jUDWnc-`H}v#Lb$'
    ';#pj~;jjP^C!Wbd4E7;CgS>rqwP;_A$ktieGu!}8=+*;U!<=f*@F@+U6@WjS`8^v0Wb$>|pzPz<26A@G(RfSnF3`O;pnnob-#z2x'
    'Z8h&Dz=oCWDH-;dU4wcX=F$cY_ZWCi{-;aH*=O1{XP#`%|NOGYoR=ZwvgY2HIVaCo-*57jQhv5`znGR2%g?Kgz6agw^E_}{;nMAF'
    'g&S_=3}>(D>Uz$>r?mIcoI(E1>-*=29L?pQ$Qk5$y}p0G$BA72L7YJv;OqP6?GNIt_P^>=RO$M)2DnKAzJ&<h7CU;Jrpfci#h%R~'
    '7s=?Pv6JQ9uD`y0w^^&*E^oV}9q%Vt^nS-#s~zX9cYIi0c}d&ePl)XOw(~aIJ`0TPu=B(6>a*PbBL@iIZSd?TTlaV#8QgV~hs_w5'
    'u-To5O5g4F{5M;-yJ4SwH+wi6>MVEtXu;!m+ui(h>t@$M-h=mgICx(e;e0{;JVWa5&aeLF*5j+A>0KS)up0K9(dwAl275TNX4wgs'
    '>4t04DNcX9`)vI@%s8C+LWhH&&Vf{qk#W+|{``EN`*V2OOh^0k%X#k4;kiE@?ay!Lxj!8wFtzol;YG=n7bVx-7+Xn2oeg{T{#U&5'
    'x$?&6Uet2%ut+VJSzcy8YB_XRq?QZ4=)4cL96anrMWPyKQyZs2x79AY)B-o;n7{L+G+)}fEpu;Yi;ZdCdG7nOF3bLXq@lgDaHs8='
    '4}b3awBkC!2X|bX{h4`{_THl6kUHaiT79>o%BHX9F})jA&epeN`FT;QA4~e&%}tYX{P``*cMwOD6rPu=S*`n3lmENRPu}GTZ~1AJ'
    '8-H^%yF}FT+x3ORc3*C;KD7Jt{Pt73ug~sBwfpu=B7Uw{`}wDRfB5shKm29iAO5!Q54$UxblCWZ*AqH${DaG39yb2rrGDCtKf4k1'
    '_U$^68~CtFKmS}sht@v*a5?GYH)q#lW={YM-_WMSMVsO#%f<WNW?JyzrcMr8`;={b$~Qfo=Lf9&!dq~A-8W|O*mcjF5aPPWO^9m6'
    '<BBG3e%ko-=Z#N)+4%IgHBT#|xS6TJYrpWO5U%}7tq`q!T+7RHi}lZ^LK{0u#+9DhwlZ?RAHS29tzYs^ZtncMT7vHSvqqL~u#>SC'
    'B34m9N)`3jmWtc1Bt5_4zT@v!D07Ft;^)I(@yp?_`0emlbXh4AV?+DIhEAd(V`yf-(9B8935<;F7a2K;?S+Af{Q?s$4AsrL?B=?y'
    'tBu=L(sP@cyXiM?^&md>4x1NT{c9G7iR19OZpQRh9rUlZH`+ILDcj>7d8_xWz4_hh*Rzre!xa~~*O_0tzx&;7>+W8>=1TOT1H8Yj'
    'yq*(Uzj65r@!>E0kji=ZE8pi=9{$EHNsq@F;OAox@XN6W`0dyObf40++dR=4X>E<PJ<9I#jV|pdFUD4BeUzOg16|rtUW_Hv_9(l{'
    ')VZ{yyqr!G)fP|3_us0Z*O%30*3x|y5UEp^>c2Gw*Wd?loeF4GTdQrmhgkvlk5$Xd&KM9G4CtRx$|(q>M70av0-j}{MHQ82%5Y|~'
    'Gm1DXO>~|Uqp3_)7v0+j_f%Upn_Eb%?HI9l)%^JjdaRt^rftN>_7A1Lf4qhL7|+iZIH)wsnD;8E*egBFYU|alA;uH1I9yfRjeHrt'
    '73d!|mD0Agyxzlp-1f=?&B=A$-S@IC*Sze6wZit9?x|{o?K9U?*9P0Gh5e~(g6%cw6W0Q3Gv{^v;pfr8<(f^uEER8>_m01?;#=$R'
    'v6@)bRsL<(=W=!zQn{@yY3)r>mq<vzT^J#qL!G%j2x$}jTq4>0d};V|2F38&lFdqLxkMuQ<-&;M918VqCz1u?xG<uq%f4Np##v8%'
    '&XVU;zOLoYv&-H2S>3wD?LV(tb}i3<U-%i!D%gEEi1R9K*X}gtCE_mdK;~8VS{=;UmBVXyKyT&bF8GArO7`t^ROeSruicr=E}UM&'
    'iOs42?sstKR++Ee`89RuuJ!olh4ywk#51eU*X}6G{jbY%NuR!|-#)WGc{RU%CVg&I{JL7h&sVU9U#?&czg@u^F1}tGCM+JBu($`c'
    'Tx`8G%!xcSCvqQZxzu`Tn09$++T|Y9a<TQ&FthWZ%+3}b+L^QA&9FVQ+FzS;zUrA(R%^AR7wRQ%d<C;5%#Y$stJbwVk)|BWWuM8s'
    'rp&s-Ioq0fEl=s&_^M85R*-MYF`Zf6zLsY-FHdxK$Mt2NobdeeG<dsKJQ!?9zOSzt@7`B?&kg9H>-lp2N0U;~)IutMZ0Z~R^VBW)'
    'hNtdGH#~K3w&C%+lMQ?5$u&G?2dd%4^jaGB(@>}sew)>SD(pP3?Nngs<@ss$=eeo1fCPUHemYh&nc|&3QXiJ$G(J)blj6%gR`-t?'
    'DNo;uY^*LVJ={`QdAPYm$8deU;o-&-yI=O`;IiYLm81=aor|2yKw#g94VF4EWZ|&k?5X#fIuK^w_!Oj7k%L7Un0HKp*t2Y@Sa2gq'
    'Z)Z|-?ZvgDh*db6hlb@AiYdY-J-Wi0ga7j%|Mx%VMKr3fy(@e8&wciL+hw!`VLumPckb!A)<6Ghkfw<A`9ul*;lKau9~E)Tp0b6f'
    '?1x|f=f9Vp@`b1ThhP8wmw)`DBA!E5v;7|2d}R%tmzBB4Z?1x|Tr?OCNO_k=U+)0%fG+{_N3je8EdKi1w)@WDXxFV)@4(v);pGCV'
    'K*RGZ!$pgQxh&E5+6@{UjraCA-?bMJ=cifjZ_aLUR`ySIpSSvZcK6dXBwyM$PC5h<IxsaBAYl;%*0zi2`@&!AeOxdJu&ycAHSN5v'
    'X&cS7%X*p^^<5eD?J$mu!P0fX(hkF)7-L))W9%?$h@rU+p}7wh?AkJsTXmkkz1774bB*0qO>dGy?Gu_82&vb#V4=ZZ#xIZ2)v~FS'
    '9of6OO7-I&E31Ij_xLr<N}km6dbtcr^;s%9U+ElidzV~gF7CU)-qu#Cgx<k!Ry0(Uh`Q9v?Se1=b+-QM*sjRH9yEc754DG!Q5FCg'
    'I5~5xMW%qlV6=HlbM`T^Osx(%fo33ruo3{wE1?t*(l`}(0j785aA4{GO5YcYtKN_e+w^qzeF|iAfBSB~p2k2;->VqWpOshh-SZw<'
    ';*a7^yPec@@lP_ObvyHG;c$u^euhq`({e0rx$KMMUOHsg#k~x<w_lgyX&IBPT+zj`Ash1RVndE>*p3ZPSQT)_=$o0C0vm(<3U;-S'
    'mPiJCN^?xaTkj2QZt+$~u3|Z$2vf;Au;SndIZ)18F>%!s?~lhOw`*<r`Y)?@?H#lCwtZ{Bq_^?caX{w-EkhL_itB<Zfl$>Bs!mlA'
    'bk<Sj%c*fn?2J?MEkls%6{N&aUKdD71gUl)b?Q>UX9HGpXR!LPbqHF$f|eYr>jEu>pw$kvPF@SV4Qg4br836D8ITmMC!A*(F0;s@'
    'bW(>LBg`NYYB&j5a|U~uvJk`sL5*yMT54z1dfU5qaMk-Ju7R!I=h17f-ZF%#p}sB%(+FWLA?(DR0nP@&^zH~Y+d2fWlLMF@n(G2E'
    'g8+7E4JY0|uh8~>G=2U&PXepLaksM-URbx1)8ez)8K39-h4_4Od^SUSUHEJfpD(SC%lqet;4{K<1Ird+`GR5D4k@{^EKp4JzPL_F'
    '@1O4h%!tRcj8}qye8GSW@I|jJFCP>0FRm@w`{(Tu8G(2P#1w(}l7W~G>6Klvpnu;x7A^zjR!{3|5u1W!Z^ioFP;c}&O_S%3sCPDK'
    'w<u3eTE+0>u*(p3FBx{(kY3r98p;uN8{F6qyQ~%Lo>fcM7SP*I{nnF1FGuLTWa#BXdSzFHs6gm#aAQ04^48FMUhSA$;BP<Wt51%<'
    '0`d2f@mCD#mEC@#67jdejji}Qt6skY1fN$U_7yJeV^#4#Inqi*+Dk@SIiy#1Ws53A+BfXj3byk;9=oFLtZDcqpl;`lO->HG3Ssw>'
    'VOI_5m0dHV8ew+<JhtL)V_ENuy2IH}X9>SY>-ltY+|`J?myEl5NU!X695sl$a}3#vx^<JbgF$yVc;7Acd>y~%*%I&MK#rH_E*i+q'
    'kY3p}I%*NfXOOWK$LqG3UE%n!8upwW8OLlJdU7bni*pwZ#dgT9?B*Q9n{zE|=FIvHW7QlWnw?>6VfPToP7Y+i_3X+noH4v`))L5?'
    'Ug@V-`A)8mvE6TLy=@-C*y&-64%xMJ#KD$0En#flKzBH+DmonebPk5>5gLk~9w~RfV72yq5I9c{oNUOh?7|hpCPoJU=g;Q}oCmkN'
    'K0R>qPJr`g1kUpSPCjH;c3p~LOQi#V^Oy4kPS`Q2g|Un%05vOf%m8*iBmzUKK%JzPCn-Xd3^o(sk0#CtAX~^~$Ik#bMJK@d3j*hP'
    '0H+wTE4$~!u+7r}!1>#G0w?@SmQGKJWR;vVWq$x46E(z8Zxc(A*hGlSMsgZV*4{H&Y<NMKPWlXhQ+5KJzaem*2XM+EyRvIb3|mcY'
    'tPS`z0rOUi{*UsSx4(V@_~BBxsJI36$;t#ySxhFHU=<@-!i1$uiV_hdv0UU#W#9rx&nTKpBH$5p#bKSX{#I|WDQZ~FzOw>{uQS;)'
    'FGjctzB(KB?^?-_sZPX5m*;@e==9P^4cV35=3>}2?3|?0!7krc(&*p<==({dgNOawN+aE=H0t6UP#T?H8tEasvO8`JTfm)@G&(qQ'
    'qn$K5w2$4M(&*4(|F+V|bSsTII0uwQr<X=%$gb?79>a!r=Om2|4o<X^MhAE8+fN!DJnY|A8re>zQ5WZc(&+Tk$PW3H-BsiSuZ_2D'
    'WzrNNYIAM;eSMGW0`*y3313{vzebOhkQz;2;Mh+dW#_wJfqdIngvXP^BVY-?vKx?`B0SFFVk<z-i(<Bb$mX5rj+FByof}qC#LA1t'
    'N;>3McFmGwSG;)(i&nMdb^^*d5S=?>R_kie(!Ipd8B}ZI48iiE!IBO6m0hjm*voE)6<d*VR&{J|uq*}3&KNOg>pL^h<OrG<4Vrw&'
    'Fa28O)d6H(va29esULY$W8oUWT*Lg-a>}gWA~~K!N>P}cg`$y^^ghNCH7-!gct|>D!W;>S5bX#v)L7+KGJid<rH!ufpd7*I`SX0<'
    'I|0I>K#+yBBB|&}^gfax9g!gb+QCsU!aXb7OSQ@-&?J<z@O>W>37!X+NoPRN!93anK?j3<q$UJ`>jD84dD4LX!Ud5n$<z{9Am|00'
    'N2#-cMKVX7U|GVVg=PQ+L8+jF*Fo{n3IynpK%jhK72_>^{s|6^!_Gy{WgygBVuPiQ5&=>XXr6knsRJ(X#-|`H?1<MqFz=WGez9yl'
    '^Y=FXI^yblAY}N9V)({h*@@$wQPxR#G0EI&k!f`0^u47y`xqHO0O<WGC@q4pGAizsP{8WaI2E{Q#qn$%$ImM#k00~sYe{i<dRn39'
    '*PFPW^XAWcBq!vJYZr!A$3T@O#DfGZx8W2`js+jJwH(x_2sScU!(c#p3K$E5Gk`VBP08W%Oo_{6jm&kl#s&p;p;1b}pNW?R6JVbS'
    'F)=ZvU5JT0m#I)hfauU(Sqs3BSVq=BvC)y(;H`P%`7S-A+wr$FnZTN+C_<1={yrk)tl0#`35t_55hr3BpS*b;sy^@YzriBrZiuoj'
    'Fte9P2Bi(M(cebeTrwA-FhXH;Cc;Q=Wuqte`%6OEf?V<Uku7J$6eucCRJ0Kl6odt3<)*KYmPiKDfaaKpx856=Z7DP%xr$}QQR|d-'
    'V5K%i$bnY*quaSc{r~NKS#KjdvgThY=(S}x$z*PR@4*ak=iWYb`(Pdp41vj|tZv(KOLm>ATkwCMATv{Irj)ysQZx?GWl6a(7!i!+'
    'i-@!4OJ$$~KO`ZD1H697qA*bb_aY$~O_=09H_Z63YQZ(tvCu3gRDrZ`l1SL2w)xTyo?k6r+QEZR;;&xfd%;NVOMG|{Fe$`KHhUOq'
    'fzg7=2R-K1H$6)&@iPDi-P3b``|tv}quySCmzHz!JpUiZLAjoTACzjj%Bm1eo?%TM_~b)My;?1n!$IOu!;W3U_G4CUYuLfinY)^G'
    'Fo{2tMn#kI946(imL1r5_<UR|EmwE9{I;_HpH|{o{;555W*_6W7qeTJwTadHa;~z6y}hU0`fFmAix<oLws*m+c(IQo^vi$!=il7H'
    'kbldzzGYwj@&Ep7=UZ><Tkp$1{`c3v!PSqSpYCXBxS9gEMcK--{ttF#;`O_51T}A~w2{)2o_~7+Wl1CW<IlOWkMS7!Z1i@U5Ng@G'
    '*tSJ^^CDLW31ar}hx^-z=art4$0iSc(eFZu_pRn{gXzYtDdx3b`)#p!UY+lq|9EWh&hPri`z)Eif<f_ZZ10{mxjTCiZL^nmHMSS~'
    '?gecz9l5*gZwyNO-Py46dH(i~-4>Qli)ZEPf3l-YtGmBjJ!$KE*F7#iKli`i6lkxzRPGmOyWf7b=c4_`Z}~rd&;RlF?RsMSW~(#y'
    'plrA`t-C$e+?wA{Y|Za2w`Tl1(t0*Q%^mse#E$&dY)8hw5v@lS)!c}$CpO}1t&O-S`qyq>`9>@C(72dIZnF`%8@q1B$9w8X*zL@j'
    '?C&{u-0$*-c)44>inqJG8A!?}-2Q4q7%;EJi#6vz3pi%C!xOF2wB(IdY!yl;W1=`Kq&TljaaNDwtP;h&$`qg4TaJnBULo0|x@7n2'
    'k=?68cDY=vR`n=9wYR=V@@EA8ni1H$_YVl1w-(Ayn>BHBIuy}2yJ(-l`6VZ53>3AQ@cKlaX3rP{vHb#WE+h7uAT_AHzUCykGx2Fk'
    '>}md}i=;YGIzFUy);MdQ&KV`vRCW;y`21-;*&?{Rc%R(W`yjF|Xg!_m_eH#1rM}YNUMvZKRoyhBl%^{!7q9AzeGl8r36^i8rhkxo'
    'IwkjfnteZceB3??eqW!!yPC=kx`6M}uqjz@XiDw(#pHq6&+;AozBt&w^p_{MFLAN`uC{OL9(&2!>bDXjvy9*=eJsK04#o4PZ{HWs'
    'A2ymz?)@fF&lGVq)-c?&___Fazkuy+Li(bCAAi3DR_$TrT37V>?K0`pdO&Jd^!a+3^l3fLyes<rewp-XJw)oWazqUW+2bJl$)$1&'
    '4w}BqLDM)076-wOom+5F<7EzN#6fBW!+~&o(y8%DI5g$t&=ee%P<2$oqm78V_gFp6U9hzb8U66^CVoCIgt}kD93LiXzRb$@PF|Iw'
    '3~ODo3-UX4>EdGve?Md=MActAI#m2|8d9zJ<2=4t@#k4Uui~%s6Y)M>?fu`IUie$n3%@qK@b{({cIJDwe12<hrtS0F+Q%)Q-`e7;'
    '`20+PZv8q1a)TT?`1!rd4wZhp4^1!lWd;;qe*?O3qit+nR4%nGQ7S7KvHIX41qYRW%Z`5Q9s72T4=DS=Cb?bqi##7&_I;K@Ec-f3'
    'Q5Agcc;eyjN5B2;=(k^we*1gLw~i?uax=K}2OER1^e3r7RQh##UfL3?A8(d7YMzXRk=m+fWV4I+PRq(S**iCDUzeVsbuY`#(j$KI'
    '9~TLNoL(eGc6yOm>gh#-?;k%a8g6x2Iyd3!URPkuwY|k}?Qik5{VjfPe~a#nKFkd@%MG2yLdMigv((I4+zHH#G|P;f#rML*M6<+1'
    '1xNL`ExQx0-3#hklcdPGEjm3<xw*%#$sOLq-`Fsy>sygJzAg&7o@nZN0SJ|rqF1bF&mK_pq+d<y$Sd8iwDP;s)tMvZSarXU`$$uX'
    'n)lwTw(jo6B_+{qAFyAoTrLTf@3>n+-2R3KP|o(Z+{ah8zvBteV?Pi0t?vhX?fU`0_x*tGAx-t(6TOkj-bmH0><(`<wyhkEuTps{'
    'I|BoaZ7WCP$yD9S?vOfT+se`SIiHTitHtwom-K?aRuU|8F$2Tg!V+9?b)}zOLL`MOrGLS9Sx36{FDG=elR^8HJ3TgVd%RuS<k&{%'
    'NjJ-OUEYy(en+qCJEjT2j&GVJ5j(!?Iswq(J#DhG<6GJU=hiLLY(?{X=2_9sZ~0sIxBS}uEq~v>Wtw$qe#@;)Z|8T->zup3uPp}c'
    '`krYb+G^&*_Mb{G=b)-*f7<0_pKUi;#i$KNZ7^zsQ5%ffVAKYqHW;<Rs0~JKFluv+QJeiR%-SQ!N?}fRn><6?@uNjKl^m9JrO%1w'
    'HSRrL?-ft^Lseh5T6^+J?^fblReY1GPif?XReJ~99jE3yRQ1(*dx5$>UB@p{!}sg@$(4P?iVsEz_{bsP#him1jadJ|u>K_t@lZJp'
    '$g`*hZb;wXmcSuv_o6L&W1>;-SKs1w)@cD%cjl^zFg^DKfg190c5Xo~=!*-c=borA7B;*Hc||$6S~-~ZKU)JuZA?lLWgTU`xvWnI'
    'm>z@r1Q~9J8Bk>*2VJ(xn)W%~3H2dz^WX<C>RjZ~8`ing(F~8n(*fn~x@;XhdQb%L=o!J$GaY6)C!TO9-YDMZu`CCM(Hug;>s0x6'
    'T$NCx8n|j@Mn3~$E3Kw^C35q{burzw@#22#d2wBgW^KH<uRSlWi@B<e7x#Pf#cdvdGhJKt?Y@}e`4MjOh&p~>Okw>9w|PXp!Y`)i'
    'euUdRqAueXQ;=UdAa`Vz=k#>X^J-Cn)1A|cCoT@OzOqT3-LAhejmJryY5921P6^jy^RAAt)1#w8FM;eQxT499JlgIV&mz&!_O;xH'
    'cXE-ALa?XF@|&K$ER<cVnd@h@axME-??4ANOM@|4Bh-RUsqgwNZKNR=FE8R%&9dfv_Cs>m(Qn;3&^>7~?WAkpsav*e;#YF}W<C8%'
    'Zg$vI!nRE)8l=1P`JD`O;DfmnsyKv$oJC=x0`5gZGMX?6hP+|M4?HTkraA^&FD4XBA;L)_!Ody-x_kX6F8W<dT2);1JMZ_m7<{sF'
    'qnF-ojd79hEEmsN7a^zmUXrvA(5AY6U9h>XPt59oAG1a^>tBC8`1SAkKmNWwC3`O}Juq30=|}fsHpu?4dFxBryTT4m7uS5DGHW&F'
    's4+6q?;~TR3ur3PgprQ%&luU}x3TG;^S2SG%r=GGGDar(Ix;4?fQ||km}HANM#dCxbfgw%`r>PmO#d7e<=J>CKb@p+zfJ!9vr+AV'
    'H}XB0PSPbDY()OWn06xthmCVX*1)z(8#A4(*E*&R%Aj@Q-k}XXu+~<sFC5vPIUD9mb}R>BHru%D`jPFQMQ>j5_Dv_*0_$z!`m!-?'
    'rA5S#D<pjY%^k&C@1uH0@zN*2yQ5vhZ&$E}uUD{!->+Z|-J?=3G6sf_<?=C64Y;K{a4T}l$R<%W;g-%YvdAqX8+O%zTe^eVBDaib'
    'o>qkjZ60jcIJW!YhkX#@<v6X`n>nx5QFNqH@+?-6FJZkE8T53dJCZ?T$Gs<Od~ez2tbZ`lUHZPbsvWWsw+h>o|7=`)mEzeial0PZ'
    ';Dq;&Hx~wdPumDgj$qPZiSo&z#gEnJF5c4nZfMew0EYW9*8y1ODoGTPz97Do;KYFy$B36OIL1R71}2hJ^FV-?5GP6#nRrqdA`b8$'
    '8Kfcw#JYc`QB-UIf(<~h0mvBp`%KrV1Sif}gx>88{NpYE_uAFk(geSNm3!)|VWZPMb+xcT<DPm-*jR2~9UXF3UR`dDnf*b-W35`v'
    'iVh<WD5pHnmjviWVi{8bjx208>^)7AkP0}=k|@#Aj|1g_M=4n%SOCXf%(joS{2-m>r{*kwOuBPDAFHr6FPX@#E2NyDW#g9r*Z=vC'
    '|GOTC>wdOd?PHtx)Tj5UVzl$lly1J7({*s|$4URHQb9TCt=y`Z0-NecdvtI90v=wMOIj-y-erfsO9?dTP4*h>Z)qW7&1u`es`WN#'
    'qY^rBHMUg3wi5WvX|vQXz~PORX9pZuk&as~0ZvWCxxk&z1+HS5cE)j-^)1W#c9_S-WNBHlw8Qi#<`~Ozj2&hTF*SE2HFw~_mUd`4'
    'Y4Z#nt+o!B$oww&_s6SR%oZknBlkzvFT|Rj?ocW=98(K+%aioL90LUbIzdH2z~SE{@vXreg`jRCrCAUPP!FCEhVp=Lu&l!*h~p&S'
    'nwbeUD#1o2*r)^>m0+V1Y*d1cO0ZE0HY&kJCD^D08<k+A5^Pk0jY_al2{tOhMkUy&1RIrLqY`XXf{jYBQ3*CG!A2$6s0168V51Uj'
    'RDz94uu%y%D#1o2h`qx`CD^D08<k+A5^Pk0jY_al2{tOhMkUy&1RIrLqY`XXf{jYBQ3*CG!A2$6s0168V51UjRDz94uu%y%D#1o2'
    '*r)^>m0+V1OqOD!5^Pk0jY=-HQHebm<>KXS{;ZcXa60TWcI?CC<5fNDJGvbfUTSGARJ9cz{!cLTzN9hYFPWd^ws`r?na1yn7w2cY'
    '4`we5we#tlUESitopx^@$@5$~CQEta{m1KF5-=etiIrjzu#glnQyz;HWm*Ixj3)#3Q~9*-yZ8>y*$%;+mwETyg?5XU5X%rw8v{?J'
    'jGGwv5A<^s3IXRJY@MH9w|cUfj|s@e`(o+VCH4I6-5H}<_m+&sa^ASNpU>YNj9CK{s+l!QCXBG)j*>g({ezJQ&L2p0&tv`<LLP|K'
    '`T9H11>Vo4W2mcagEhUpEnt)7&)L89Psf{fiTci2@owS&)bc%}ajauc>X2(sb0T9A06jIF^dOob@WIyaSNh-Z6w0TRKdzTli#AIQ'
    '?$hdh_9RvyJQLXcfEnopUPOFizzcv=CNOd2!7h5tGhA>F_Em|PhxQ$P&J$20A&3inBitQVAJ2}i0<`v<rCU`VihKqJgs0*Jlv!Zl'
    'XoZti!GAmsK?WlMGAO~SVJ1|dOAHN!rIb{0ph2hG^(11YXH9wCT|8g|At_!LukfPJGqsQ(X@vRLrfbm88L;Kt!1lP0v!7)C-+?EZ'
    'cOr7iWTxid+;#a80QMkdG55F@z*S&2^T>dyNENZ>4P$T?0Ci$!8P6EP1Oa1`go1$e7sY`%q6vBley#oMbpM=oO@rCTR@s5c)=NCO'
    '{Ea)Xe|ib7m%nibzDF;C_3}6F!2IbWUjLW|-Hyv`7%P()N_=nzh(}`|_zpY;utSW7&$Z-Wrhq<XBBp^a;{-$qOsx=pDLAO3$@JV|'
    '?6QaLhMq#}?071XOeUcD!SM{hD}rs0bR?~A<*^igC_`$%>mgngM@i(v{~|cybP(qe%z26ycz&@a;<2^&t}fri&*z0um>0*qIOfGM'
    'FOGR}%!`-j#V^3RPW_wp`ItJ!#it{Y+{S)^4MUSq<-xHiBN~U11V@Mw6@(-K;)hbf9tdVQIFSN8I1bDk`-<+Kj`=ZYPsB7^`%rWq'
    '43%V_Zokle*0_0gxFE7Mm+v3T!r0byCu?i3P#<Z4aAx&11<`Rj>zkOrx@^c!S{H7$mDmMll{)t=SMOr^?(RXh)m#25p9fY{L1MUH'
    'BhH1oW(#AS0w)%us&$(3Ie1%}=1KFv74y71%J{QJx8T;1iO>2$o|OdV6Ps8V-P?<6cO14<#DybqGVO8#i*rs_s6gdU^SEc`f-JwS'
    'GClkHEmJyG&@3qcp%9zlsB<>_)>`LV%BPQ8J<Z*zveA}n1k7LUYdkz;Ja?aMteP)DV(s!aJ8Abl3kat>t$)1PAJw?;X?lKcvI4(y'
    ';;MzbPr>1qq$1n5byw<4TBdtsFMFp-MP&KgxVx<u&)@Sk7wIPb?8uO6;PkIdOTh)3h(2}fST#42M?DYpTkLZ%j}7OM0!;_1Gf9Nf'
    'phhDDHU?2XH6DBf4tXM}ij-gy%t)>&6&_)U=V}LLFXAnwaD{wJX?mq`ym@_t0b|Zpax90r5*ry;GdOA%>kW1^N(~}mdFLm*^sBQV'
    'fSYX2X&wJ>(<*-@7jH$$y<D9gD-t=A{;4HaslNf#+06uQWdQ#MyYOY5ALyVyq#7YQIEBUnq^)**w}p3hfQ~#byt6e&41%(}7~pj$'
    'h|Km(^SEs+R933HSO#ah$~2DmYu9YtS~UEzfP-74k*;wiVs{TE=XYw$jxO#0q*E_s6cCChAf1aB+xTO8@Xodm8E5wwyNq$DBn@Ih'
    '5)%6s^%qbLW>ccVIQBrElvc6F;E6J!z>na(N)2itvaz;=1$=UFi~}2`c!v~9ac?<shwt8=ey&^sd`}m>Er8NL-I@Nw84!C6Iw(<*'
    'u}99>pca8yiQ+<X6B9#Y!b9Nk5HzbFnn;8m6I5_!{D=lTWT7!K2xKUePzIi?%-D-RU$Z*wQn%~z2>TCaqBa+=J}N#?xW+789i++!'
    '5w5|@;+qRu53e(BL31(59XY!(*>ePN`*sx}*|Qwg>-gydsjE%abi1$vd$8vZWDZOxf7;CZY?-qAA7n}&(L}f?HI+hMte%>u(QuQ7'
    '$m^I49poktg*<(+9ZK(55$hKp#7OTTeA-IpJ9z$Z*~&xKL6N^;k<V<93kWXl%RDWU`3|DxHA7YF?*|@8je>ODXJ-f4yX%&t4vNT7'
    'j&9&U>c!LC7bJF8BNQFaI;ahldCdD%5pnfR&(f3o3_)f0^gIYS{Ns0&*$Xh!axR|d|0A+S#7?Un2OU;ae5mdQR(J5xhXiwOwO9@p'
    'WI!E@ppld5zYX3NVI#=eR|Okcvo-=p5IBOsk*<LwU5VF?Lq{Ih9P3JQu}};P#Re)A+autixGQi#;aM{W8^}J})~W}~e=LxtD#O%z'
    'G=GQF*RF<TC_dQV*!33u%JLpZ@3TP))zK}D*9Nlx*sKT;-sK(MPtDT40vS^ma6MMQVFeskz+nX(R=|y~0&Y41PjfL_nDn>qk4&Lk'
    'h5tEhcKS%=HJ&ke#^4!)XAGV(<EygnsAK$Ba7dOPd26w*I>lFNB|c~`t=gU9MZjX^WGMjenCXNhOot-$ctVUHAL)M5WMjMQFHiY>'
    '#Nv|v^6YqjdFi6EE!)fcQ|cGBm#;w3{r2*px5C6o4LqQ{FcL&C9fc}Lj4~9|h#x596Bb#vfcK(-RDrUu>M28%*RH+17K}zR5?oME'
    'Yr^1ZT*Lc^47kkmjL<<y6J_Atxe0<W3^Y)OBwE=>dqj`6m#-iowbBe77>Wb7m*=oOq45F&RMXmzLhUQin8Jg#`<zhj-Y<$Z1(g|k'
    'icz6y7)PE?v>>!zdwDxFBn;q)0YxohX@D$TQYkbs(j!_XvCwuBiARKQ+lGcJp~mMi3j!4j9v3yhJkmZqVZh>n41Ax3ii-eN7fNmj'
    '&|gqE0*x2?0b|nWm{Q*W6A4dyz_y0Tm;|QQ+G3fA*a)iNz_)FyA{oa%V_x8!C?K&8HSBvB6OS1a84YifkT5iM>I%q{k#*PRC!z8M'
    '?2{%@5P3WzuqW^w>T53v2xG8fSZ&0pRLm28;&G}x>BZcJAbG8|B|fl5$igT9!ypMj5BU^UsX=CVB>X7iO3=g$Jeq(+CdyZd;FM@L'
    '>IOc+u>d$00LKEPTj_z&O9_WIs9Fk2H2~cc5Rp)jXV0e~)(qSQJIxW!gG3TZVK=2@U@Mv+oh8}A{O>?ZRl^DRN-+_r2uyAP3dkqK'
    'sK6J%x+LL=2mfH)1EmCH1w+A47`R1=;WFgF#t}6;y)d&-e)<T{auOWYJ`WWEZI>vX9Ay_1#DU<_ngXzaG>VNMaq3F}zCl94M1a%U'
    '_8?3&s5t3+u%!l0Z;+o@`6i57lDvaMuNui29Mnv5%7ihnk1<#pz^-5ifdLlFKnXD6z=gA}9D_65BtEQABz~ZQAj+lXOGpk9>M>B8'
    'ei(;dNT`J}9M^(^MmBe5L>Ma2uqI|2h#!J~nfS3*U`@cr1A~CxG7*Ux2OEUjX?j$HQJS<Qc?aiJ#TrWAn&PoZ;E0GQC<!=%IVfid'
    '_EZ!ylE5oyAC~3=lSg2X#+*?uq)&kxg%BMNzv#f28_)2IV_p0Q?%ZE%{M~gM%w6!0*A4$v7#;GS&D7|8=Ej9H=kX~Dov`ls{Gl#b'
    'mvzIstoGC!+Q&$m0j9=~_ETdncAU$#%v|Yys!Bao(o^`M#@v*Ud7Stn3c-RhMk%;=5-|e1<!Kf%Yr_W8FfhD=2wc0E$0i5}Y=lrc'
    'gnt&#O&N4{RnG&J_6NXxo@oTj^kU}(9UTnA3E7?s^YY=Klj-=AgBdX=eX~2b;>ZA*EDfVy#jArOeAaXPYE72u!YSRIEc0ukEosR#'
    'T~fN9Y}SkolD{rq=C8tLfGj0nfk~IyU!RAQXxt~SE3;U>q=RLS?@Rq@J?Ra)9@o=>+gWE^&$KgcEehTo=hg_GRh?lAgG04w+eP4N'
    'K!$BMNomUt8sqG_&pMN!S|<?)p9D4jSn;+?WlzE#5C~`x{i-vy;_8qxE9T5EeG(6q51re=xEv@bhoaYN_FXLJ;x%7e^XXC;QSUG+'
    'Hauf?@X{w4Go7S3EL&pMp+h&dqse(4yve|m^LogmRGyr7taz}J@%r$vDI}bhUR~y>m0q1Y7rQ@5&6<aYEQnC>MdpbgezW6&9)7*C'
    '3pd}n+QySa*nPvpYl@a$aLxW=j}&EAe6h-J(_r6@TvF=gl+}_}X0t*pdjp0$ai4A0I|@ZU<8<3P4(#WF+#Fez(_8z}bd(CWJ@fdp'
    'M8g~-@n|?_n-{hn=W=fN{H&ukiv+Q{0xi9|lJFjw7$eB~PwVF1cn`U^T6ycJhrIQ0X@mR`ejoWoxNqD``hB3}C2ya+94<q1nK{xi'
    'h8+h^zg}J{>)Hmqe-?IQS(p1PcM7w5?@ZD`qwwJ>y0$x$>=f@+bI$h3l$$Zt?pl_ftbNzqGOVy`Xf&bGghmq@O=vWs(RA}h)8TPi'
    '5Jaqau#P2^cw*I6W5>0Ol-hyjYWLs4q#bs=IK69QisNmsJ2lK2t$6oF8TO{hovOigcAmCAXc|3l_g-0GyV;xHc)+_>x6Ccd9_yHy'
    '_G+!vL?o~HKkytqJ7!V(jQi9C;4zxS7%zz_xNkC~is{(aMkGcUmdHdY!h;wr3?jogj#=cuFKYH8G63(9#w4YENE-K8Sl?j4J_}Wv'
    'ksLM^vE3ll44n7Hx@+W-#$MvxHfLG)yqKJ+*;l1LNX?YNm=5j=)al|J91=Es9w6^oVCR<u1Rnw9c}NiQG1<xtjcI$Mt4w$YvB6HQ'
    't_$1j_%qN{YiO#uA8iHk@5pv1w*R(Wp!06gt1n{pCGFx-X8*KqFj;RXK?k_e4KH9te;*b7#f=+#;Drvr?d^F1tMf0kI)5-W>;PWe'
    'o(-|m9xLs!(!N5aeG9cvtqE@j6(AdRfW<h~n`?KthaD7`4ZVlstMu!A7dt4U>7>c{#nc~FZmGWIsqS~OxK-ErM8}F@1HCnRBeWmP'
    'P%p9AssR7<^SIpaPanYMG1xo?o5x`D7;GMc&10~6Oxxx$*D`UZsxjQj%$?!p93iS6QT2$bM^rtc>Je36>=>Oq(&{C)S|8P0gW<LT'
    '2b0J2UDKg&tF6&uDQ&loY4~ID@v211_v4ycwTotSMys}mxf_x8q|@4mQ4Fbj0@7KvW}zN;w%{{^ZO#gyB(AGaZ<U;TSWw`vbWU74'
    'g6=+hw+-9BJ^fs{$zgl(qqhZ6{ikyne>i1*P_jB8fsx6th{?lPfe%ZG4}JmhXzat$$eCuLFd9DBl1JK4d~l3o8u&6!Kv{Y+3aJIF'
    'C%#{q$rpdVrm^>;BBQ@rCuQ<@L=*;_>)Tw^`f#*R(5^+$z!sz|<{sC=(_CxCBLik494gi{WejXPA!*EPPg-N(Kp|jAl28y9(I|G&'
    '&_M)kV4JIr!1ZAQqR?HE(50@X6}Wj?uw;Nao5^1X)3pxeFMYoqg71jr`uz)qvQjdqYl-cm{cf?u4*nj4B8JLY4mHMgYK#u%sh!ps'
    'w7JG8OQ?0NF}mm^--X8Lar$X54?~S{a~h-l7`xLNgVoX)eHs2;v`g<oDfBojwwH&YQn)px(80Kk(@McRhvP9=fkBC;80`7O;oVR>'
    '+?aOgU`o_!?ZD6BWDHii(n>pY@ciMxg{U2FO*^!oiF8^!_&qrkgH=7XQUD!1e>mheDu6L6fDA5sIUtmM4G@%RfX2v__4flZ5=zId'
    '9giIhb{~asv>two!f}H~M=#K>ed!3#YdrRb_ZsZ^!_`Hj2Dmj1&<o~lUjsxJk&o&Rh&K3p3|gFsT45Ag!L{AF)G~%m8*waTKn@?C'
    '!~3scSaUd5F<=$LjY?Vn1q@wD*^QSlJgynn6<;sEWrg$UQ&&&(x2E&!Cu+XTZ2JaN>kF^mo>jm3!QQy{f-kb3Q-|N|^jbRndQ+Fa'
    '`L6oB?_<}LP5IutW`D7;F`EKnyG?Sbg=?zwM~z9GRflUf7<E;9dX@DNf4sTrInCItu)AUdhC6YeZPvSr5h(|9b9^Ynp)}zrg7Q%W'
    'B^u^R#;X-6$m0_)>Zr{kL9DJoORugZyg2a}dlO-ABJ53sy@{|l(alf%UC&MWmG@rCU-*8xGbYLQ5=eKabX;Mz0aiUmrH6~36i)T6'
    '?wr+`6<pbqtAE!T)11+0LZb<dCN!GRXhNgu=8dMq<Fp`%c!krJkF2_C?6{VZQajLG?f#vQhrZ-E-uAj%PwX9bL~s7IE=KX<?A>l='
    '?*dU4&)?mS!Cxy0uOepfCRRy17EGsS?NHWz0^et^zy9@vPIj_e>w(EGpPMCpb55z{Vr>myu5+(K$7<_d8>!yhaT~keNGEo5|5BZ3'
    'ckM9E4q`WlZJhmSErRT`ZBPTc``730H+t9JV*xmS_(JCo@7V5jhCmKHg|TBXx{~16Sck9o#&w;}Z^f7@##Aw;iZNA;sbWkOW2zWa'
    '#h7Z>G1Vc5t)6H6=~;&LUe5U35e$kQa&#*O>M>B?WuSgP>b?&2{>u96V2lrAeB+7nO$$a0Z@~Wz!`+T!1{Vg5|6M`+uPr~8rW%Lm'
    'N7fBLvf5!Y+DA-lfUpy3A9lLfc+j;3ZMqN3TxZ~=NRMHv0#g;3s=!nQrYbO1fvJka6SF+;U)6xe2y&&MXiYDx5%jX{c3n(jqN*u3'
    'rrsr|6M_Rv+zn(RIug2aJaVNS`va9^H)*yjOq||CI<S&9$D}zX&2L;vs^Cv5A2|a?OL}$TD{7Bz`V~VCs+w1u0Kgc%^vZG2EvvK~'
    '>`<9zF6{JqCYZqVLPG{s-56{>cpNALKXSplL_b@Fw$sZ8l<YS>ds(E@mhv9Ew%-rXUCmMgq`04=3eR_)#rOBRA=?khVYT6=6jLtV'
    '^kiCIQMgqKY3BM_O^Vg~-odwV2AhF}#)KBFKpg~Mq8*8LWCQSJiEL&1b=n@3Sth)LU^Gvyt_$qu@n;~EvmunTpez5TZ{Os<?V<f8'
    'r^*+x`eMb(o|46fKkSNpat}Wi=5wt8t>hgZ*Y%p|Q%0i@6rdNASj5r<N^?o2(8NfOXqm)9D-tFi5xz}^g({)O=P?Tc6$>60c^L?~'
    '>lAY5CUOkH3$aW@Yy?$4ji`!LB*9%{%!4IH0f}{}!$gKL@t84@(IW5@5{4$>aqNYWk)7}YVv#Qti#!+`CcXyKjD=AU1||t&9s3j*'
    'QYTXJNcd62m7s|icr;;@B+6Hb;FM^>rBC3KPS_Apmx#JV)MW+KWec@XZMYT-M9d_oOc(<ziz5-jx<x<?QLzk^kYOkcu~-tODI7<>'
    '4@OJk2io|Na$}4(>YxT-y#*Ym1EXqKynNq#=qtrUprSx|LMrAHVpQM@5Jr;l#Djk@?)i2Dl?s%fu#lR>a2awC%!ryZ>|qDS(9l&l'
    'eeYuEdzzEjBwEK21tsLcnG5Pv1|*0=MiMx(wQoG`!zM-~(J^O~3+Yo%0wKgv#Bz<T>A*nf2yIqK@uDtk8&7pJy63iC=jl#V^vpdw'
    '?FYln^{&++beadCDn^rb%p<ocN92AT>N()l>q)jAiX7(j_F=$BMR8<N{OYpYfn|9sNU|CVvb~XTW4N{IwC%u*9yD*$>g*anXr;1w'
    'cpaq|nWeGRFCY%hos3QfSXY9mV0|RkM`C>>)<<G}B-TfkfgO(@X#)?gx6J)6+<ChVIG8vr;m505fL)iqp8F$10$LmEvwAyzEf|et'
    'B)FiS)&w>>;2Mr#U_D|y&j=laG*QODVoVT(VW4C1nLtLtfCnpcZDgiSwXi$$6CU>JJPQOTA~+Gji3m<aa3X>e5uAwNL<A=yI1#~#'
    '2u?(BB7zeUoQU8=1ScXm5y6QFPDF4bf)f#(h~Pv7Cn7iz!HEb?L~tU46A_$<;6wx`A~+Gji3m<aa3X>e5u7*@aH3s{T)e!^pY?Ln'
    't8mM5NIqWGv%aI-*n}mBJeXo?3ykplt6F@?U;PX0`+MFkF-MN9_S+|}KYg<+U3@q}dizM8=Q0b)y1o7JdM5%FD|ecMz<@-7*~)Y%'
    'LXRiJ_%WRf2%*gEc2M6t^m0n{?z=mii<TfgN}~vTM&DC5rkD^gLWoBLcm^}n%dcBK+3u$kOF%G}?Yg9%zd4ILhh=8mmJ|Ele(uOE'
    'ms;3>uT_x~Mp$r1$sP0l!N>#W4>Y<DeD5!WJP;en)ZltpZ^JPKJl_FQ^vl}<_E`R${Y(FJd}^1d?<{83ZPenQpdm9F$2#_;4!O3D'
    'n*=IFL7+ipfZT#|3$}j0(*K61&|pe~$MuqG(PpW^9az23p2X_OF3yjHFiQKLm-w(yFVda@O{GF#6JPNVB!oyq2??o}5Dl*ws)Q>V'
    'g7!-cOM7;HJUhAyP`_`MZdG|G@>wh+cwpkgk`3rsP_YXB<8c`HK_tRNN>Go?gh~NhQ4mR>lvHt`dF1)<^hB)mtSPU%iwDFEOYypR'
    'g%^FEsfGN=-Ptu9_~#7Ra&BOI+{f8ZvSakXMadz?PM6Ho{F}QjKOf*=p)BSeydrQFxRX3GaOO}&>^a96927#*m|4a%hA=_kR3fn5'
    'ETU2DLMwy1Kx@Y<-9M*kQ)}edDqEl3(|!zs@;7d6$V~gOj>_M-wGlJz$1*B^<JPv(v=@WxF%2~xm)kH_CINlxgBK2(JNAL^IMXZ?'
    'M#JY?@<{uMub7Bw;LA7x5%Odd!Y>6+e19@McQ9kPG|q02)Wy-u*WSCj+@2YXfej37U|<6S8yMKYz(z^N_w3en>ffx-$J8+{J{^hV'
    'zUB(pFf<8O9vq9{(8M9QLqWiZ3PO?q@k0r&4in7qDB%J;I1bDUz9ii}9TAIzSe)}<ajHkRT1O^6>j!y?=A3Nr#kD&Q*EXm3oUTxT'
    '%Ae+O&&&l`e%p@|y9j2b(3xFax}4S1+$rw5GdLd};H}hr2?DZHuU>6zZt2yz9^CywD%}qcnW8WFB0Gl;zuD2thhJ~h%jP>bi}@rG'
    'cHi*unxdr_T(iGeJG*MlgqCHqSa!;!`p297QN#Vh>9$=>w{61jIL*6j(r+t^s~En$r{`{F(B!@v$C-ysGVO&&9?r+c`Pg`jmrwrP'
    'Hv_MIL~V8BwjSUXTNYen%Yxmk7}kxJ)Qx`DaR3LDD{KpMY0N#k!uHR-IK_b6iycI;g9vsIsn9{Bv(WqH^ac3*+@P#~<tXws&^JTl'
    'I<S(+;^C!DSF5$*D^1HVj}u=+Ave?*rC_m1#0c<~r&+{c^GP5LnC?Lk1)dl4*aQIqwh>B)@XtXJ+0Iifsv3K!<a|{i6WaY~_oLmv'
    'rKPIKTUi8aaCt+20R4g9SUwL}4$jr+C!n8ze!|WA35V*aAVu6{RKmq3ichS$YHF@6K`=qM!6nq$k|Vjb0k`B9fI3^d()S1v6b8(e'
    'U;hY^^~8(jqe89-xU{2Q1{rdaKds#`lj~><*5ULO1e5K7$vz!5SBvNGIr6xuxWNSta=uOf%24QV!KNh$ouoZPtMyjN+a?@D;$xqK'
    'PiQ!gRII?&B1s~Y76EXl7yCr{)Ohd_VNoJ&{7^6nZW-5<3XibF28rit_9C(kKi#jTv^`7XeiiE*4A`@xN{^PqUMse<hMFy2tt!m+'
    '_sG0F@osz8tow&du6%~xMLUKKXj9EP?VfOGGw-@8ZMUw}3_a`C-HM}mp3#*kL+E$n=t}fQ{m-kP!Fe5SYkxCD(TgqcVzcCHju%^Z'
    '?mX48bKohW9TdG|TW;)Lh25*Ldlh!C!tPZki-r3hbl1Km%C*h*s5$5k$A{tgFdQF-<HK-#7>*Ca@nKEFEzL%EVq;5eY&rCh*{UPg'
    'uq>s=(Rr;6l1tmiG0tG`WG7pn)Pszkyr4Pk-He`WYkOYH=*h8`Kz2N@t@Sa|KR;jz`h}LDcg%{dPs09L5$jm7juq=zE7Y-etQuNd'
    'VOvv0AJf-M=Yp=bhE=8{*)qD|kHyEU5-CrWprljYoGrzx9UuA!nZ}RSvU{I@*Rww@VYj}b_JXF!bc@$cFW4H3dgmfEdq{0~$j(U2'
    'dr*bn+$JpG$v>UD{=?-5cET)9eQqsyTyr0HcgIBov{m?S%IK3$E2>8^vGoZ^XQBR%vFygrU5$e<ATc|9kYi@t+=1SPt{pO0#`4>w'
    'PWdgJ?s~s|^TnU9X%w!g-utiCF_}DWtA@9ypDQ=J^Gx`h68RYXJqFF57V#4*f@@O*9n7vdtq5pOieNBALx}<y?D@lm&Qa*cCG?qJ'
    'TUYAaw?;fI^{h<lOXYc;*E?>I4oG>Fh}$>@I~cP#3V~=nTn`1}Mve#*fSbpqy?v|+IDN*CIQ69nsQ?M#ARo47UnM$Zq4Yfl&IGWP'
    '@RL~iCX6acgjdTjtOStj%4+NY;nvD(?BMysLHST6+=5E*y)ZOVf<YLCno<@7F{J?kb&!P0^QoVN3>=NX4+$I>i6oLz@H`|_TA9F9'
    '3mWm}DxnO8*p*7?07KYHC3Nuo;lPrp5^h2z2pTaJCPsvziXvlTromnay@Vxxtd*t=m;{)Kd)BFp84r!e;gu!TK#-)8O7PDcK5CE3'
    'ZWlgk|NP<L=P32JAoW}s0&)y|5r$g(U=8?6zya^c*!MZ1-1&`B4AKwAfTtK0U^&K-rxPs*t!4-W=kN^%21#|4TI%3E1_c<Snz#uy'
    'kuX?TAOqiLq2eL{MG;DFNFt@6fl7=A_BvzIXz(L^1Meq1?FA|{OvWTImDEId4u4@_09!|DqJ#Gs6u*vY;x^QTpM=U6U_EFO1(C-i'
    '0+s@>2#BPY1cWhA9-ufPM!^~N1UR0YDo=VbkAi^nN@^my2rNnmRRf)9f)3te&{By4etKLBlmm7yzpeJ9$L(n);juMc=>k?ybBBGc'
    'dt%t}niDl&W-L|HEG@k{XT#ke<dd&6SaQJ^Sq0?bH#-%9hhJ}?vYYR!Pn|w?P1!`<y=(Rt3mdb+U0N`&Tx#*2H#XKk-dq>sX8VN='
    'G`ne_*^J+@^K93o-&Ph^&=C1R&)rBT$U`-b{R10xNh&__uvsrQ>&0WdxLI$J8F<}2UF#UPI9eI&T5+^;wK=nO96%hQj3bmsG(veV'
    'PJyG6vBVrp%yCq5|D%%E?eBl(CG`0V-)~yvFSW?3)k|k^RhvrCW8Khjd8+bx_m!Nl3S>gNAMJj$`;QKEujUDyMc&XKK!2b&md^v0'
    'gL5_d3Fs%FpK!B&!l61UND*J%<lG}`u9}*|iMZX)RIR!tM{;X8>9((3!O^yuEyrwm+tIe`0iZa-c8?;%%&g6yhO=pZ;V{~shtUGD'
    '7SG>v<nc-ZfyB(l-9cYE4Uz^8a=uOf%24QV!TOgIS3t1?V?Hci0G^-2v(i4X*=O5TBN*q$I6ubuG0u;1evI>DoFC)-80W`0KgRhn'
    '&W~|^jPqlhALINO=f^ld#`!VMk8ysC^JAR9R-Au7aK8@0|H=jnRH5vxj-0pcS}M)iME)ylT5wdlosvJzh1K_A{1x0M&xUF=`{S`1'
    '&91G}$T7pFBIRz*?!9qO20euRz8Tf@267+z(YM`Q=5$D^@W>I#D*djSQG>fuqkmlM=xs%lbaz)M*m2HkX(86+zNfZxt(okydgz@_'
    '5?zqc-9X32)+wy25;sN=&9pAwf%kxSs0G`LdVuW>=V0cK@cY1Yz<uLh(1!y7MtFO`$WXrgrQ%Y?7*-k>@^WcW$j;z0hWjzsfl7!<'
    '5=Ephh%Y7aJYWz;yo3SyLK+4pl2r3Rz`n<c(ss_I6o!Zn;W8DC7zF4cKo0?W2+-?9<CLR?yYROVA&&_8OY4l;uq5R2!~qMmut00%'
    '4VW92LjsK*vn2Z28fLF*@g=9o7hnx5b1!=NP0wByZs3=h>u0rc)t;+&5K}cvjkT-@wxCM(U1!3#Hx12xNDhl(f8|WDl8d*L`%4O5'
    'E%#)<b#FB(&N%D^AU9FaW=N{>K<69Krdm9*d%|JBco)pwcI!%$H)q|tTY=(N*gZt%BccEi1r3n-C9;*lC#Q(mUnBO*w9p^`{!`n8'
    '!2VM-LY#lcIRBktvYYPulgSD92lhoixy2s_CN@C}*P!2u_MyX4Z%}UsNt6RI!Q4OQ{xSEDxqr<4WA1;y0;%z_=KO+!BW>sNyD$L?'
    'c`A`iCV|n$BY_*tP9q&j9!D{crSL-;Qo{)+UKB@3<ir1>*iUp2AC38s5}Qy+@uE(Bn^XEq+>+^JGvF8v)nX{l&hX-qM||wB%OrGH'
    'OE3^PCOKuo7|@+@Btp=vA|QsSSO!YSFa#;I(#SLoP2&4WED}G^#*b7WZYaJ~ov5MjX-;C3z<C-`II%oUI2`LTAVCx|l7vy9edBRo'
    '1u7zmjya=TNS|_0(n8!`G^;voeWjQHss+juQZb(pqXJ)m*+deac<>L#J>PPx0^<4!gYzvhT!tJ>sEC@OV`tMm?o%;{Q)WGmHr#T|'
    'l37Ps4$j_!BkNWx{ZAZC!xs3|P=f94&ss{bouqwMHxCh(R1Ag4Bz>u0;0}%5mevNzrS0PgK`q_vWb2c<@=jg{khL4Cx3)d2%MU_e'
    '`+&fn<b!%yjn>CV|NMX$&<n+YcFc;cPs09L5etd1kO&KjDijiRtQuNdVU~;DMP^FDzdv5p0vtyR>v;e1dM5%FE9ZW~#sg=73CeUR'
    'LXRiJ_%WU2mt>%6>nVO#AFLDkES8a{;>3>vaCWJUwkY_I$6??H;KL+R0!J_tDn*H*;N*BxOBDy2N1ng-b<>!Fu$%G|9`@>-aKv#V'
    'jvH~@h~q{aH{!Sv$Bj5{#Bn2z8*$u-<3=1e;<ypVjW}+^aU+f!aomXGMjSWdxDm&VIBvvoBaRz!+=$~w95>>)5yy=<Zp3jTjvH~@'
    'h~q{aH{!Sv$Bj5{#Bn2z8*$u-<Gy?xw|#fHczK&Y>*c1m^_Cr&e7ve>eMh(92ut8|a23_0N_Xw;#@fa0w%_dH79WmD-#(J(xy<|u'
    'r;vZV-boLZ7=fqbd&;gQA>alRj|M>yGDE$|fH?9jf(L44hh9!|+<kYJR^bv%$SIeBCne)N4w5J^Tm?D^rH&J49J}jQPj+;`#1e3^'
    'bnB9O{^o)ZIW|3Gu$<WU_H##WxzxgXEmqr17-7L3C3npG2O|%hKQQk;4!pk*@<8m<G<ZBVVqy7ULixm@PxQ;%0`^$`oc&Ax+(1!)'
    'd}`q~YVl9dJ{gUH^?))V*Vc@du?T>m8YCUW4HQSP_4}3nH#~)gQyM<5msE>3Oa1Q;y?UQLi51AsB#0#?EcSw!fwg8lLBJEDCS+QJ'
    '<sND&1z747UdP#RA!ru?-fxnKKqpb`r#;?3o*i8UsJ1stx2im(1z3tQ67U31YXX`m;95j+2;yZt&j@&PnkZxZm>XcxFwj69a3B;o'
    '4&blB(-X1Mv!=Z6E*`LOLW<YLE4=9QOfBR`9ur&FMEslqTh0w^kNY_LNp?gXIGlN%h0`T7HUH+WTL)>CfwRE_4=e;_?nj~al?aUR'
    'WbFH#Q10F@iZx|{A9{*Wp=lULo=&tNR2<O+y~K^yZgjeTlT@7+uPtE_uz?JGpM{Ep%@&2B<c1`cTLYCC4-QAhq|q^@z5yl@p7sJ2'
    '8YW{Bm|AP=AeK^VZGI9eU%)<T5(SaR;TQ&Y8urQ8UJ?+-V8yW7h*7B+2xH=Lsyyk%9JrG6T5Ibd5OJ_J7m=_A*-`Fnn?jiOlb0xe'
    '<JQLAv>%JH{Eb`NjMIJ$(egKLZHi8Nv2P#K9K>;b>w6&d5{_OER4oOi8i4Kzh)5{Nv*%L~YZgfkx|YCskVqma>?WMkVEq};%agN5'
    'e22@0*2vQyV;2*|f#A}b0<eKJij5D)y)Q*bf$U%+z-bMJib^!7IO%(^r3OxKke^ujCX6O$PnHaam#Bf|II0B&jhG4(Bf?ODhBYzM'
    'K>W~4SmMW8fi(de4-5i&%S0q*9BdE{7M-M8fSzbc@(#|c;m{Pjc}$m7Enj=@#@zOVEDWOeGY{~Z4dyQR2ZQbxJ3#N6{h=;c7lZT|'
    'q{kqArK!zl4?m=VyR~VRMtqC4OUOJ<d=Z6UK^dbI+&hUFf!*>ni<q@x18Eo-UO@z|UCd(>1OzrhC>_E-ceM+G_z}d9Abte#BZwbC'
    '{0QQ22jZ`uh;E%k{H&jkEgE6H&5G9^E5>>2ry<Q4Q9naOeR#$kTg?P!9nO$-*ka48C>%Iv9;GOLm_xcU?|b=e9|E_^yQ`i=1=+=='
    '%UM0mU7T!V_*Uz?`0xO4rRGbJ$DPKF)f#S=UY+|SyFW;+p@)Yo_E7Le=D;6*v*WlPe!Vd#H{aPeKTU+)H$1$iXz2yl>@OBJX2XP*'
    'Wiyzb-P&p|R{3oj?Awt`jy34iKi=%x8nRho7mo%Ecj7+VtXI8v=&)6ywL0b=$jz}<fsdbu(o~@@+pX3Pf0k&N8qH2GmvgpxVcXv>'
    '=Z4SEI%=~>5UVTD(yJ>8?}254UFEQ=9Cnq%u5#E_4!g=7%Yb#{-Cj=Dw$IN^jn=Qc>}*{ov}r2~{zv9=md>rH*2%Xl@<Al`HCG@s'
    'p-HInD3AsG=rEE|5HO;GkR-q=A^87<31)bda1jPR2T_ZCMF*|ta#^;b4g%F2vwbq<CQP-vmSr?+-!-=iE9@E?OlUBn!Gs1A8cb*~'
    '-Mqncc$^jl5Sxr3y5KP4iB(sP8`m;YY6qFC-M{mJMVB1I+g^8z88W*!8fMl?{<L;8Rd(;y!4Q(04R(Jzs;w5!-`$Qq7Gr~4az5YR'
    '^sg)_2^VadU(iVkxvkbkBySLK;5CBlC%E((_o)fMTQrF=UJ_Gq-DF4=)3Gh3NQ^Knk%>}-2QgR|M22x3v&dm()a*rMc;C~VNlF`$'
    'H15o>zQKUq8LD(6IqWK8dq}7m`0tDL{vH{RDc)^&mUX9$$(4`HyzsMopxJ$weiZgNX6@>yll48_>!uIZkxcRG0G5Sq1hGO9D-;K6'
    'BRG&+=m4^gjc%{F(d|HN*a1Kt`)~K%f4lF&Z0&K_*Vg&IXNq@F)q_(bPEU>Kc^5k<g25RsP4@7}h(sLIgkze99@ErppcwW6!9F0^'
    '2L$_oU>^|d1A=`(nhx_EU?AHC%?s~lAX{4l>skh~A=(+y&WLtKv@@ce&*m6)JhiL!QN1^{%W<(x1^L%*8<DM(8U9#&yeg5hdI?Y~'
    '6`2{BZ+2GKA7mOo+T;Iy{$0=hv}E7<irNdBg5xY?Y_@}q_&k7#!R>b@Pu;^I{>_np0nhyD+>#$oCG12o+`+%K)Nzeh+T9mtl51<K'
    'xM5yTIz`-%Vldhhkj~-^9ZT6QpF4o1Fd!>s9MEG6?L+3un0=enDZiyd)b7`BzWDPs4aXMM1N_z6t&_)XrSSIjbLB>4oe7CkA|Hdl'
    '$Dm2IB7Q<eaCM5H{TPVTih$Nq1eJnEU2BOh8en#zC3+m%)XT$AOWcB%=wR5;X)VE;Yl*UiNY`4Ti*BS{Xo()@jP>#`)DpL#B|7NI'
    'dRj|(JvkMFf#ymSz+lfGF5QSie+xq2zH$9&q37ptAO-_<w$cb4Jb$<(IckI(&<Gt&3^=V3{Bt-9g8_tFX@m}*KOD{rHNv<wLWTgT'
    'sS-NqS$J9{1Z66rRG!x`-Qx+c0V$6XaT{l32SX-DArP$xaiKum$Pp373fu)s{DcTtQ|yO=a;6zr5E?$$l1JK4eDFbH8u&6!5>I+E'
    '3aM>u1eQf5i3radJZfL#(k^(^{`tcd^Ptq<g4EO4qb%kg915Q18q|Jd3=hCQw75M6ZbKv_jhUUjXbjkfL~24pL0ANiWl$}86kP-k'
    'r2{~DC%K~z-eXV{eE~l`t_8}WOmVaqj`qUQUO3tdM|<IDFC6WKqrI?p3v0Kqb_;8_uyzY4Rp4kZ9PNdpz0hDng9!~LG?>s}LW2oM'
    'd*Ns=9PNdpy>J4_1&sFE&T4I@IN8K=mYvS;Qp>&C8b4F#UWHE8*1a|oy}9EyB&?A>?CAcb`tb9y2qFL?H-{}ud$44mZDTXg)kjw!'
    'U43-*(bY#+A6<QP_0iQwS07z{boJ5IzbRLL-*;cTi@&n6uOZzC9NTxHWBYcFeH_eHHq^-ix{nOzY8YQ|3_G|i;_Sjp6Nu5xIBP8q'
    'rQzD)#Q_IR<Dh9AG+klPbY~HQ%bA*5sK+p-jxlwNsbfqXW9k@F$C&!ziCLbYu4=%Kd6ejYvl451S&iT;ZMUn}38OU&r7M|oW2*Z;'
    'ow*uV;?6Y=b$misj_InjV}GEM9Ki(S+nPx{<dooQ+We23+xr`rW|WeBDX9XKsC?uM7%l15g|DbRw&_<4xTtDgjeab7HRE~9DlLb4'
    'RHm5=JAIxBBr(0*@v_whm1E<kqc9=(kqh1>`q_pdU)ADE9#;7RB4lX^y7rr%y)0Y{NHf>ZYUL^uR-k~jnx%Be>R<{AW!`lb<}Vqa'
    'yH~iescMmnx1zbh>(s(lRf&76NwJpSJO4I5VKc<kn9z+CsB;To!4fiL5;1KLA}pixAeh2atLp-5c>Ea%J!}X)-066_=}$A6oY2TX'
    'fBBS-CqEK`aV>7FkhGn>-a#Jvzyd`03Bpehe$og0#Pv`gLM?OvPi?Oj5HNkAfa$^5umezNdp1O@a8JB2*vQm!4BIFXdwo;9gGrGa'
    'sl#%L68w1IyV${WzoB<=bTt5ldsx&(WaE==TGHRPlRVuCRh>ERWIq^gG*Yc7*EA0`Rg4?$SPO1bj%bo~Q0IVCuO|_9C~}x@*@tKz'
    '6~&Qd>Z{9g7xQm#1m#mhLAE#7ZwymZowi+!a32zz(dz6PKcJwpd3YVL_{`E+>K71)=I(x51FJICv33n>*RXaCYuB)L4Qtm*foR8%'
    'w1FqLTIPNi*?9~9{&-ajFy*QbOU*9Ha9h^+`W$u^yt6)2Z(G<c`w0(wbuI@&$q-70P%?y)A(RZEWC$fgC>cV@5K4wnGK7*LlnkL{'
    '2qi-(8A8brN`_D}gpwhY454HQB||6~Ldg(HhEOtuk|C4~p=1apLns+S$q-70P%?y)A(X5El+3=qT)e!^pY?Ln;cLrSNIqWGv%aI-'
    '@Pj2tIapt63x@C?K(^(V^fIl!Sf}_|yybnywp)x9Hy(Sl3tW6S`FZ<Dp64=)3c4+|5H}NY%4Oh5$vBULBnk{yfeu2c<HSYA2ZOU^'
    '<~nHY9hy1adH3BN$wf=xz6q#D5)}|@z5~m{rfCi)iuB=iRDRv+$+i!iSS$xv$(xb(j$@>rzrCmP=~J{^#&0>N+}qC`2t>klHUMdr'
    '<b)9x+);AJynitA!1)6U??WE^g^&kgX{W#2S@(1IoC)<O4t%0t-WIUs^5^Vd`sW6Ta<|!CAj3aFO=c92wH-ena&5gg2{em>K!e@@'
    ';ROvBZ2f+v{|yhK(UeA;Rn?-@QiD&hdY?Us6-d*>6LCZs3!}&nLXW_;)T+Huaz9G^m?_SM2}1A<jWi}8vGo`!xR3@sNp4u$`|jh}'
    'kyd~XezT;j%26>Vg2(U%3AgEiK-eb;1{x9`9EZ{it)`EdCut1lN(Aa!`zkU_ia>y(hNmZDrDsh!-(5T)np29`#Vfq%^Gq$|NAA<E'
    'fto*Oz?pLc+v7gYev+~9fuE9tKb>}&srff|-TG)#22L6e9RCoG20sdca3V0mld<n}Lb;)!<f2$p7Wkp37%&PAV>s;+EeI7y^g=JW'
    'gSGdS?%yO;u*GXj7&s+?41Ax3ii-eN2MlXSBBh|9;*AGKEo0K?*zzjui}17;s1O(@CV{E7whppewbte*q4Gg+!BU8V$m4LRdtm;6'
    '5u&{$09l0<!)hZ&rD7h~IUc9VlU~e0Yj9p`Z5^al57y=!S!+rj<<7Q-*R&sNuKbN#TcOi_OxyA|Zf${1`!Q?F-?+75JMG0<eoW(p'
    '$Mvo6fzV4hZ9Gu56qITJx+fqap&-wmPeH6%Bsu6>5(gk)L{ivIIGVs7G@zF!XV1Y87Y3~nqCLhgCWr&Ur9E9?18Ec+KjPGvBBVfe'
    'ICW*@C5cdp1{EiL54O~(I05;Im2bjma`uGzaH*0SX`G{4P|%2}Ffk$w6=+x!GY!NK!G8j4Q7f<}VB>*7KyR6d#EgRt!of%ar%HgH'
    'Xi4%8&a2@PDZ6<bm+C8Dd+)~F_IzRl<MlHG`kD>qF8BvA#TSYxzHWc03)aOd8LX1QDw#_2zRw<hNCS6k(<%-67Hbzzg~XG>sxd(f'
    'm`f54N^8ri(1YXF>KPpq&LR`QAt^&IumM&@J#c{%PqJO@a%K2Eh5$YU@F9Q?0elGHLjd0yhRY3>Mf|LvkS&_+watpx9xUGWsj$kd'
    'xIzg->SqY456_rmE1bi$!x_>JTWon1b<pQbq!a}Zb68jAelNeRvZBt{Z<$wC0hVqdtVJbznPYDq=!evO&Ya}ZTE5hIwqY?xm(|nU'
    'McmS#{ng&i-ss^WceW>Lz69yD3)zs_Zb}A5OSeJ)c(Xq`cB2!k``n-rf8|S63wfV5KH5O$eyx{YywdfPEA^yuPj+`$EuO#Ut0^*}'
    'oDN&hPj!D~#uQwzfeg|~8kwkuzs!MDho_wf)*;tmY=MOvD-tLQT<XPs!a=iWu((vHi~%!@8y!Z8mw*yvB$Tlq3K4~lBW5om3yW04'
    'be2Lm8>86PHy9AfRt3j-h{v>eJ2e9(zgX|_k^Y9_-Qrr;sKQB$o`)w|)K!t}>TI66Lh!1)=-(~W@85tVU)B)7<9)|!XsP;(+EG_K'
    '_-3c!Wg7GUwX0pX4g&aN0fHt{pL1MTOm{Zq4CK7{J*IM>X-gu92Y%A&`8^7l^%GFjWrS>&Bo&<5HeY_`1aSK{_|Z5mHT6Q~apHq='
    '<%Sxg6xc3F#0U?hr&+|Tc^62-z<dCw-1A}{0}Y5LL!opC|C}5s0eF)XhP^)M9!O28d3*Y~a<JXKCA}?x6Mj1V@<XTS>YeWgWamF^'
    '+<Uf6xAYG(rLII_ywZ7fqL`*Ktt`&n`JV}0iEIuj9qF6xkUABZv?Stw{pMFDG6jT3iMe2j5eDq)ngY{7g7-=Urh{6WL&86<`OXF0'
    '(Cxl<w|kE?hq3`a;T)bdbM{E~*|wncyVU)6U%$n#OnV%^iL*>Ab8OWGe7eyC4Wk3_=y&_WS&HMxwy0na(KQ~(HQt^g(QZCO7b>~?'
    '`4Gqubz>#)*Q<??%a7&CruQ8ieExr}M4{O'

)


@pytest.fixture
def captured():
    raw = zlib.decompress(base64.b85decode(_CAPTURE))
    assert hashlib.sha256(raw).hexdigest() == '88fbb433d3c142151fab490939268216f8f01a0564d3ac44ef694dcf842365e8'
    return json.loads(raw)


def source(captured):
    identity = captured['context']['request']['commands'][0]['args']['publications'][0]
    return next(entry for entry in captured['history']['items'] if entry['publication_id'] == identity)


def replay(captured, *, overlap=True):
    entry = source(captured)
    items = captured['history']['items']
    history = items[items.index(entry):] if overlap else [entry]
    head = captured['history']['head'] if overlap else {
        key: entry[key] for key in ('revision_id', 'publication_id')}
    bundle = bundle_from_document(captured['documents'][head['revision_id']])

    def boundary(item):
        return tuple(bundle_from_document(captured['documents'][revision]) for revision in (
            item['previous']['revision_id'], item['revision_id']))

    entries = {item['publication_id']: item for item in items}
    resolver = inverse_address_resolver(history, bundle, load_boundary=boundary,
                                        load_history=entries.__getitem__)
    records = select_inverse_records(AcceptedRecords(
        [entry], entry['records']['commands'], boundary), address_resolver=resolver)
    return bundle, records, history, head


def test_exact_root_config_redo_authenticates_without_rewriting_history(captured):
    original = copy.deepcopy(captured)
    bundle, records, _, head = replay(captured, overlap=False)
    before = copy.deepcopy(bundle)
    entry = records.entries[0]
    record = records[0]
    assert record['derived_writes'] == []
    assert [write['path'] for write in record['writes'][-3:]] == [['clips'], ['registry'], ['config']]
    assert set(record['writes'][-1]['before']['value']) == {'app', 'clips', 'theme_overrides', 'tracks'}
    assert 'clips' not in record['writes'][-1]['after']['value']
    stages = list(inverse_stages(records))
    assert len(stages) == 1
    assert stages[0][:2] == (entry['publication_id'], 'inverse')
    assert stages[0][2][0]['writes'] == record['writes']
    assert stages[0][2][0]['derived_writes'] == []
    assert stages[0][2][0]['_inverse_effects'] == inverse_changes(entry['records']['steps'][0]['patch'])
    request = copy.deepcopy(captured['context']['request'])
    request.update(base_revision_id=head['revision_id'], base_publication_id=head['publication_id'])
    plan = execute(bundle, request['commands'], inverse_records=records,
                   commit_request=request, evaluated_head=head)
    assert document_from_bundle(plan.bundle, source_bundles=list(records.load_boundary(entry))) == (
        captured['documents'][entry['previous']['revision_id']])
    assert plan.steps[0]['patch'] == inverse_changes(entry['records']['steps'][0]['patch'])
    assert plan.records[0]['derived_writes'] == []
    assert plan.records[0]['writes'] == [
        {**write, 'before': write['after'], 'after': write['before']}
        for write in reversed(record['writes'])]
    assert bundle == before
    assert captured == original


def test_exact_later_overlap_reaches_existing_intervening_guard(captured):
    original = copy.deepcopy(captured)
    bundle, records, history, _ = replay(captured)
    before = copy.deepcopy(bundle)
    later = history[-1]['records']['commands'][0]
    assert any(write['ref'] == 'parent/clip:bed' and write['path'] == ['volume']
               and write['before'] == {'state': 'present', 'value': 0.5}
               and write['after'] == {'state': 'present', 'value': 0.25}
               for write in later['writes'])
    with pytest.raises(CommandError) as error:
        check_later_steps(records, history, state=State(bundle))
    assert error.value.kind == 'intervening_write'
    assert bundle == before
    assert captured == original


def test_exact_public_inverse_refuses_overlap_without_preparing_or_writing(captured):
    context = captured['context']
    realm = DetachedRealm(captured['documents'][captured['history']['head']['revision_id']])
    realm.documents = copy.deepcopy(captured['documents'])
    realm.head = copy.deepcopy(captured['history']['head'])
    realm.entries = copy.deepcopy(captured['history']['items'])
    before = copy.deepcopy((realm.head, realm.entries, realm.documents, realm.accepted, realm.prepared, context))
    with pytest.raises(CommitRejected) as error:
        create_runtime_commit_service().commit(context, DetachedPort(realm, context))
    failure = error.value.failure
    assert failure['http_status'] == 409
    assert failure['details']['reason'] == 'intervening_write'
    assert failure['details']['code'] == 'intervening_write'
    assert failure['details']['hint'] and failure['details']['suggested_command']
    assert (realm.head, realm.entries, realm.documents, realm.accepted, realm.prepared, context) == before


@pytest.mark.parametrize('damage', [
    'config-before', 'config-after', 'config-shape', 'root-before-config', 'root-after-config',
    'root-before-clips', 'root-after-clips', 'root-shape', 'root-op', 'native-before',
    'native-after', 'config-direction', 'effect-order', 'native-range', 'effect-range',
    'source', 'step', 'ordinal', 'cause', 'extra-mirror', 'source-history', 'native-order',
])
def test_root_config_history_rejects_tampering_without_mutation(captured, damage):
    entry = source(captured)
    record = entry['records']['commands'][0]
    config = record['writes'][-1]
    clips = record['writes'][-3]
    patch = entry['records']['steps'][0]['patch']
    root = next(effect for effect in patch if effect['ref'] == 'parent' and effect['path'] == [])
    boundary = next(binding['boundary'] for binding in record['bindings'] if binding['key'] == 'inverse.stage')
    if damage == 'config-before':
        config['before']['value']['app'] = {'foreign': True}
    elif damage == 'config-after':
        config['after']['value']['app'] = {'foreign': True}
    elif damage == 'config-shape':
        config['before']['value'] = []
    elif damage.startswith('root-') and damage not in ('root-op', 'root-shape'):
        side = root['before']['value'] if '-before-' in damage else root['value']
        side['config' if damage.endswith('config') else 'clips'] = {'foreign': True}
    elif damage == 'root-shape':
        root['before']['value'] = []
    elif damage == 'root-op':
        root['op'] = 'remove'
    elif damage == 'native-before':
        clips['before']['value'] = [{'id': 'foreign'}]
    elif damage == 'native-after':
        clips['after']['value'] = []
    elif damage == 'config-direction':
        config['before'], config['after'] = config['after'], config['before']
    elif damage == 'native-order':
        native = captured['history']['items'][-3]['records']['commands'][0]['writes']
        native[0], native[2] = native[2], native[0]
    elif damage == 'effect-order':
        patch.insert(0, copy.deepcopy(root))
        boundary['effect_range'][1] += 1
    elif damage == 'native-range':
        boundary['native_write_range'][0] = True
    elif damage == 'effect-range':
        boundary['effect_range'][1] -= 1
    elif damage == 'source':
        boundary['publication_id'] = 'foreign-publication'
    elif damage == 'step':
        boundary['step_id'] = 'foreign-step'
    elif damage == 'ordinal':
        boundary['command_ordinals'] = [True]
    elif damage in ('cause', 'extra-mirror'):
        record['derived_writes'].append({'ref': 'parent', 'path': ['config', 'clips'],
            'before': copy.deepcopy(clips['before']), 'after': copy.deepcopy(clips['after']),
            'cause': True if damage == 'cause' else 0})
    else:
        captured['history']['items'][-3]['records']['commands'][0]['ordinal'] = True
    before = copy.deepcopy(captured)
    entries = {item['publication_id']: item for item in captured['history']['items']}
    with pytest.raises(CommandError) as error:
        validate_nested_history(entry, entries.__getitem__)
    assert error.value.kind == 'history_unavailable'
    assert captured == before
